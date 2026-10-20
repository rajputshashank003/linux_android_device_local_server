"""Authenticated, single-admin OCI app control plane for Termux PRoot-Distro.

This is deliberately a trusted-image facility: PRoot shares the host kernel and
network namespace and provides no resource or security isolation.
"""
from __future__ import annotations

import http.client
import json
import fcntl
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import app_db
from execution_engine.config import DEFAULT_CONFIG
from execution_engine.runtime import detect_runtime
from hosting_runtimes import local_oci_backend, runtime_inventory
from hosting_registry import RegistryUnavailable, UnsupportedImage, inspect_public_docker_hub_image

OWNER = "local-admin"
MAX_IMAGE_REFERENCE = 256
MAX_INSTALL_SECONDS = 900
MAX_START_SECONDS = 30
MAX_LOG_BYTES = 1_000_000
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_IMAGE_ROOT_BYTES = 1536 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MIN_SOURCE_FREE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_INSTALL_SECONDS = 600
HOST_PORT_MIN = 20000
HOST_PORT_MAX = 29999
RESERVED_HOST_PORTS = {8000, 20241}
LOW_MEMORY_WARNING_BYTES = 512 * 1024 * 1024
LOW_STORAGE_WARNING_BYTES = 1024 * 1024 * 1024
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
APP_ID_RE = re.compile(r"^[a-f0-9]{32}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
PUBLIC_ROUTE_RE = re.compile(r"^/hosted/([a-f0-9]{32})(?:/.*)?$")

_STATE = Path.home() / ".local" / "share" / "myforever" / "hosting"
_LOGS = _STATE / "logs"
_SECRETS = _STATE / "secrets"
_SOURCES = _STATE / "sources"
_LOCK = threading.RLock()
_INSTALL_LOCK = threading.Lock()
_CONTROL_LOCK = threading.RLock()
_APP_LOCKS: dict[str, threading.RLock] = {}
_RECOVERY_LOCK = threading.Lock()
_RECOVERY_DONE = False
_PROCESSES: dict[str, subprocess.Popen] = {}
_JOBS: dict[str, dict] = {}


def _dirs():
    for directory in (_STATE, _LOGS, _SECRETS, _SOURCES):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try: os.chmod(directory, 0o700)
        except OSError: pass


def _runtime_storage():
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    return prefix / "var" / "lib" / "proot-distro"


def _runtime_alias(app_id):
    return "myf-" + app_id[:12]


def _source_alias(app_id):
    return "source-" + app_id[:12]


SOURCE_REF_RE = re.compile(r"^github:([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)@([A-Za-z0-9][A-Za-z0-9._/-]{0,99})$")


def parse_source_reference(value):
    if not isinstance(value, str): return None
    match = SOURCE_REF_RE.fullmatch(value)
    if not match: return None
    owner, repo, branch = match.groups()
    if ".." in branch or "//" in branch or branch.endswith("/") or branch.endswith("."):
        return None
    return {"owner": owner, "repo": repo, "branch": branch}


def is_node_source(app):
    return bool(app and str(app.get("runtime_alias", "")).startswith("source-") and parse_source_reference(app.get("image_reference")))


def source_directory(app_id):
    if not APP_ID_RE.fullmatch(str(app_id)): raise ValueError("Invalid deployment identifier.")
    return _SOURCES / str(app_id)


def architecture_matches(image_arch, host_arch=None):
    host_arch = (host_arch or os.uname().machine).lower()
    image_arch = str(image_arch or "").lower()
    compatible={"aarch64":{"arm64","aarch64"},"arm64":{"arm64","aarch64"},"armv7l":{"arm","arm32v7"},"x86_64":{"amd64","x86_64"}}
    return not image_arch or image_arch in compatible.get(host_arch,{host_arch})


def node_toolchain():
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm: return {"available":False,"node":node,"npm":npm,"version":None,"architecture":None,"message":"Termux Node.js and npm are required for GitHub source deployments."}
    try:
        result=subprocess.run([node,"-p","process.arch"],capture_output=True,text=True,timeout=5,check=True)
        arch=result.stdout.strip()
        version=subprocess.run([node,"--version"],capture_output=True,text=True,timeout=5,check=True).stdout.strip()
        return {"available":True,"node":node,"npm":npm,"version":version,"architecture":arch,"message":"Termux Node.js is available on the device architecture."}
    except (OSError,subprocess.SubprocessError):
        return {"available":False,"node":node,"npm":npm,"version":None,"architecture":None,"message":"Termux Node.js could not be run to verify its architecture."}


def _host_port(container_port):
    # proot's documented -P mapping shifts privileged ports by a fixed 2000.
    return container_port + 2000 if container_port < 1024 else container_port


def _valid_image(value):
    if not isinstance(value, str) or len(value) > MAX_IMAGE_REFERENCE or not IMAGE_RE.fullmatch(value):
        raise ValueError("Enter a valid OCI/Docker image reference, for example nginx:alpine.")
    if value.startswith("-") or ".." in value or value.endswith((":", "@")):
        raise ValueError("The image reference is malformed.")
    if "@" in value and not re.search(r"@sha256:[a-fA-F0-9]{64}$", value):
        raise ValueError("Only sha256 image digests are accepted after @.")
    return value


def _validate_spec(data):
    name = " ".join(str(data.get("name", "")).split())[:80]
    if not name: raise ValueError("Application name is required.")
    image = _valid_image(data.get("image", ""))
    raw_port=data.get("containerPort")
    try: port = None if raw_port in (None, "", 0, "0") else int(raw_port)
    except (ValueError, TypeError): raise ValueError("Container port must be a number.")
    if port is not None and not 1 <= port <= 65535: raise ValueError("Container port must be from 1 to 65535.")
    health_path = data.get("healthPath", "/")
    if not isinstance(health_path, str) or not health_path.startswith("/") or len(health_path) > 512 or "\r" in health_path or "\n" in health_path:
        raise ValueError("Health path must be a local URL path beginning with /.")
    command = data.get("command", [])
    if not isinstance(command, list) or len(command) > 64 or any(not isinstance(v, str) or len(v) > 1024 or "\x00" in v for v in command):
        raise ValueError("Command must be an argument list of at most 64 strings. Shell commands are not accepted.")
    env = data.get("environment", {})
    if not isinstance(env, dict) or len(env) > 32: raise ValueError("Environment must contain at most 32 entries.")
    clean_env = {}
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key): raise ValueError("An environment variable name is invalid.")
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value: raise ValueError(f"Environment value for {key} is invalid or too large.")
        clean_env[key] = value
    if sum(len(k) + len(v) for k, v in clean_env.items()) > 16384: raise ValueError("Combined environment values exceed 16 KiB.")
    return {"name": name, "image": image, "containerPort": port, "hostPort": None if port is None else _host_port(port),
            "healthPath": health_path, "command": command, "environment": clean_env,
            "public": bool(data.get("public", False))}


def _check_port_available(port):
    # Reuse-address bind/listen distinguishes an active listener from harmless
    # TIME_WAIT sockets left after a clean stop; a plain bind falsely blocks restarts.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port)); probe.listen(1)
    except OSError as exc:
        raise ValueError(f"Host port {port} is already in use or cannot be reserved ({exc}).")
    finally: probe.close()


def _app_lock(app_id):
    with _LOCK:
        return _APP_LOCKS.setdefault(str(app_id), threading.RLock())


def _allocate_host_port(rows, *, requested=None, exclude_id=None):
    """Choose a port unused by any persisted deployment or host listener."""
    reserved=RESERVED_HOST_PORTS | {int(row["host_port"]) for row in rows
                                    if row.get("id") != exclude_id and row.get("host_port") is not None}
    candidates=(requested,) if requested is not None else range(HOST_PORT_MIN, HOST_PORT_MAX+1)
    for candidate in candidates:
        if not isinstance(candidate,int) or not 1 <= candidate <= 65535:
            raise ValueError("The requested host port is invalid.")
        if candidate in reserved: continue
        try:
            _check_port_available(candidate)
            return candidate
        except ValueError:
            if requested is not None:
                raise
    if requested is not None:
        raise ValueError(f"Host port {requested} is reserved by another deployment, MyFile, or another host process. Choose a different application port that the image supports.")
    raise RuntimeError(f"No free hosting port is available in {HOST_PORT_MIN}-{HOST_PORT_MAX}.")


def _resource_snapshot():
    total=available=None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key,_,value=line.partition(":")
            if key in ("MemTotal","MemAvailable"):
                number=int(value.strip().split()[0])*1024
                if key=="MemTotal":total=number
                else:available=number
    except (OSError,ValueError,IndexError):
        pass
    try:disk=shutil.disk_usage(_STATE);free=disk.free;disk_total=disk.total
    except OSError:free=disk_total=None
    warnings=[]
    if available is not None and available < LOW_MEMORY_WARNING_BYTES:
        warnings.append("Available system memory is below 512 MiB; additional apps may be terminated or become unresponsive under memory pressure.")
    if free is not None and free < LOW_STORAGE_WARNING_BYTES:
        warnings.append("Hosting storage has less than 1 GiB free; source installs or image pulls may fail.")
    try:
        rows=app_db.list_hosted_apps(OWNER)
        active=sum(1 for row in rows if row.get("status") in ("running","starting","pulling","queued"))
    except Exception:
        active=None
    if active is not None and active >= 3 and available is not None and available < 1024*1024*1024:
        warnings.append(f"{active} deployments are active while available RAM is below 1 GiB; monitor model and application responsiveness.")
    return {"memoryTotalBytes":total,"memoryAvailableBytes":available,"storageTotalBytes":disk_total,
            "storageFreeBytes":free,"activeDeployments":active,"warnings":warnings}


def _log_path(app_id): return _LOGS / (app_id + ".log")
def _secret_path(app_id): return _SECRETS / (app_id + ".sh")


def _private_config(app_id):
    try: value=json.loads((_SECRETS/(app_id+".json")).read_text(encoding="utf-8"))
    except FileNotFoundError:return {"environment":{},"command":[]}
    except ValueError:raise RuntimeError("Private application configuration is invalid.")
    if not isinstance(value,dict) or not isinstance(value.get("environment",{}),dict) or not isinstance(value.get("command",[]),list):
        raise RuntimeError("Private application configuration is invalid.")
    return value


def _write_secrets(app_id, values):
    _dirs()
    path = _secret_path(app_id)
    lines = [f"export {key}={shlex.quote(value)}" for key, value in values.items()]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.chmod(path, 0o600)


def _redact_line(app_id,line):
    safe=str(line).replace("\x00", "")
    try: values=_private_config(app_id).get("environment",{})
    except Exception: values={}
    private=_private_config(app_id)
    candidates=list(values.values()) if isinstance(values,dict) else []
    command=private.get("command",[]) if isinstance(private,dict) else []
    for index,arg in enumerate(command if isinstance(command,list) else []):
        if not isinstance(arg,str):continue
        if "=" in arg and re.search(r"password|secret|token|key|credential",arg.split("=",1)[0],re.I):candidates.append(arg.split("=",1)[1])
        if index and re.search(r"password|secret|token|key|credential",command[index-1],re.I):candidates.append(arg)
    for value in sorted((v for v in candidates if isinstance(v,str) and v),key=len,reverse=True):safe=safe.replace(value,"[REDACTED]")
    return safe[:4000]


def _append_log(app_id, line):
    _dirs(); path = _log_path(app_id)
    try:
        safe=_redact_line(app_id,line)
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            backup = path.with_suffix(".log.1")
            try: backup.unlink()
            except FileNotFoundError: pass
            path.replace(backup)
        with path.open("a", encoding="utf-8", errors="replace") as stream:
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            stream.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + safe + "\n")
            stream.flush()
        try: os.chmod(path, 0o600)
        except OSError: pass
    except OSError:
        pass


def _tail(path, limit=65536):
    try:
        with open(path, "rb") as stream:
            stream.seek(max(0, os.path.getsize(path) - limit))
            return stream.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _ticks(pid):
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        return int(tail[19])
    except (OSError, ValueError, IndexError): return None


def _owned_process_alive(app):
    tracked=_process(app["id"])
    if tracked is not None and tracked.poll() is None: return True
    pid, ticks = app.get("process_pid"), app.get("process_start_ticks")
    if not pid or ticks is None or _ticks(pid) != ticks: return False
    if is_node_source(app):
        try:
            return Path(f"/proc/{int(pid)}/cwd").resolve()==source_directory(app["id"]).resolve()
        except OSError:return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        return "proot-distro" in cmd and app["runtime_alias"] in cmd
    except OSError: return False


def _process(app_id):
    with _LOCK: return _PROCESSES.get(app_id)


def _serialized_per_app(function):
    def wrapped(app_id, *args, **kwargs):
        with _app_lock(app_id):
            return function(app_id, *args, **kwargs)
    wrapped.__name__=function.__name__
    wrapped.__doc__=function.__doc__
    return wrapped


def _manifest(app):
    manifest_path = _runtime_storage() / "containers" / app["runtime_alias"] / "manifest.json"
    try: data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc: raise RuntimeError("Installed image metadata is missing or invalid.") from exc
    host_arch=os.uname().machine.lower()
    image_arch=str(data.get("image_config",{}).get("architecture",data.get("arch",""))).lower()
    if not architecture_matches(image_arch, host_arch):
        qemu=shutil.which("qemu-x86_64-static") or shutil.which("qemu-x86_64")
        if qemu:
            remedy=" A QEMU binary is present but is not integrated with this hosting controller. Use a native ARM64/multi-architecture image or the separate Node.js source deployment."
        else:
            remedy=" This PRoot runtime has no QEMU user-mode emulator configured. Use a native ARM64/multi-architecture image or the separate Node.js source deployment."
        raise RuntimeError(f"Image architecture {image_arch} does not match device architecture {host_arch}.{remedy}")
    config = data.get("image_config", {}).get("config", {})
    if not isinstance(config, dict): raise RuntimeError("Image configuration is unsupported.")
    return config


def _resolve_image_port(app):
    if int(app.get("container_port") or 0) > 0:
        return app
    config=_manifest(app)
    exposed=config.get("ExposedPorts")
    tcp=[]
    if isinstance(exposed,dict):
        for value in exposed:
            match=re.fullmatch(r"([0-9]{1,5})/tcp",str(value))
            if match and 1 <= int(match.group(1)) <= 65535:tcp.append(int(match.group(1)))
    tcp=sorted(set(tcp))
    if len(tcp)==1:
        updated=app_db.update_hosted_app(app["id"],OWNER,container_port=tcp[0])
        _append_log(app["id"],f"Selected the image's sole declared TCP EXPOSE port {tcp[0]}.")
        return updated
    if not tcp:
        raise RuntimeError("This image declares no TCP EXPOSE port. Enter its HTTP listening port explicitly and redeploy.")
    raise RuntimeError("This image declares multiple TCP EXPOSE ports ("+", ".join(map(str,tcp))+ "). Enter the HTTP application port explicitly and redeploy.")


def _entrypoint(config, app):
    entry = config.get("Entrypoint") or []
    default_cmd = config.get("Cmd") or []
    if not isinstance(entry, list) or not isinstance(default_cmd, list) or any(not isinstance(x, str) for x in entry + default_cmd):
        raise RuntimeError("Image entrypoint configuration is invalid.")
    cmd = app.get("command") or default_cmd
    return entry + cmd


def _guest_env(config, user_env):
    env = {}
    for item in config.get("Env") or []:
        if isinstance(item, str) and "=" in item:
            k, v = item.split("=", 1)
            if ENV_KEY_RE.fullmatch(k): env[k] = v
    env.update(user_env)
    return env


def _validate_source_spec(data):
    name=" ".join(str(data.get("name", "")).split())[:80]
    if not name: raise ValueError("Application name is required.")
    owner=data.get("owner", "")
    repo=data.get("repository", "")
    branch=data.get("branch", "main")
    if not isinstance(owner,str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}",owner): raise ValueError("Enter a valid public GitHub owner name.")
    if not isinstance(repo,str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}",repo) or repo in {".",".."}: raise ValueError("Enter a valid GitHub repository name.")
    if not isinstance(branch,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}",branch) or ".." in branch or "//" in branch or branch.endswith(("/",".")):
        raise ValueError("Enter a valid Git branch name.")
    try: port=int(data.get("containerPort",8080))
    except (ValueError,TypeError): raise ValueError("Node application port must be a number.")
    if not 1024<=port<=65535: raise ValueError("Node source apps must use a non-privileged port from 1024 to 65535.")
    health_path=data.get("healthPath","/")
    if not isinstance(health_path,str) or not health_path.startswith("/") or len(health_path)>512 or "\r" in health_path or "\n" in health_path:
        raise ValueError("Health path must be a local URL path beginning with /.")
    env=data.get("environment",{})
    if not isinstance(env,dict) or len(env)>32: raise ValueError("Environment must contain at most 32 entries.")
    clean_env={}
    for key,value in env.items():
        if not isinstance(key,str) or not ENV_KEY_RE.fullmatch(key): raise ValueError("An environment variable name is invalid.")
        if not isinstance(value,str) or len(value)>4096 or "\x00" in value: raise ValueError(f"Environment value for {key} is invalid or too large.")
        clean_env[key]=value
    if sum(len(k)+len(v) for k,v in clean_env.items())>16384: raise ValueError("Combined environment values exceed 16 KiB.")
    return {"name":name,"owner":owner,"repo":repo,"branch":branch,"image":f"github:{owner}/{repo}@{branch}",
            "containerPort":port,"hostPort":port,"healthPath":health_path,"environment":clean_env,"public":bool(data.get("public",False))}


def _run_source_command(app_id, argv, cwd, env, timeout, label):
    proc=subprocess.Popen(argv,cwd=str(cwd),env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                          text=True,errors="replace",bufsize=1,close_fds=True,start_new_session=True)
    def capture():
        try:
            if proc.stdout:
                for line in proc.stdout: _append_log(app_id,line.rstrip())
        except OSError: pass
    reader=threading.Thread(target=capture,name=f"myfile-source-log-{app_id[:8]}",daemon=True);reader.start()
    try: code=proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try: os.killpg(proc.pid,signal.SIGKILL)
        except OSError: proc.kill()
        proc.wait(timeout=5);reader.join(timeout=2)
        raise RuntimeError(f"{label} exceeded the {timeout}-second time limit.")
    reader.join(timeout=3)
    if code: raise RuntimeError(f"{label} failed with exit code {code}; review this deployment's logs.")


def _source_tree_size(root):
    total=0
    for base,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if not os.path.islink(os.path.join(base,d))]
        for name in files:
            target=os.path.join(base,name)
            try:
                if not os.path.islink(target): total+=os.path.getsize(target)
            except OSError: pass
            if total>MAX_SOURCE_BYTES:return total
    return total


def _source_port_adapter(app_id):
    path=source_directory(app_id)/".myfile-runtime"/"port-adapter.cjs"
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    code="""'use strict';
const net = require('node:net');
const internalPort = Number(process.env.MYFILE_INTERNAL_PORT);
const hostPort = Number(process.env.MYFILE_HOST_PORT);
const originalListen = net.Server.prototype.listen;
net.Server.prototype.listen = function (...args) {
  const first = args[0];
  const port = typeof first === 'number' ? first : (first && typeof first === 'object' && !first.path ? Number(first.port) : NaN);
  if (Number.isInteger(port) && (port === internalPort || port === hostPort)) {
    if (first && typeof first === 'object') {
      args[0] = {...first, port: hostPort, host: '127.0.0.1'};
    } else {
      const backlog = typeof args[1] === 'number' ? args[1] : undefined;
      const callback = args.find(value => typeof value === 'function');
      args = [{port: hostPort, host: '127.0.0.1', ...(backlog ? {backlog} : {})}, ...(callback ? [callback] : [])];
    }
  }
  return originalListen.apply(this, args);
};
"""
    if not path.is_file() or path.read_text(encoding="utf-8")!=code:
        path.write_text(code,encoding="utf-8");os.chmod(path,0o600)
    return path


def _node_environment(app_id, app):
    private=_private_config(app_id);env={}
    # Termux injects its exec shim and loader paths so Android child shells can
    # execute binaries under $PREFIX. Preserve those required values even though
    # application HOME/TMPDIR are isolated to this deployment directory.
    runtime_keys=("PATH","PREFIX","LD_LIBRARY_PATH","LD_PRELOAD","TERMUX_EXEC__PROC_SELF_EXE","TERMUX__SE_PROCESS_CONTEXT","LANG","LC_ALL")
    for key in runtime_keys:
        if os.environ.get(key):env[key]=os.environ[key]
    env["HOME"]=str(source_directory(app_id));env["NODE_ENV"]="production"
    env["PORT"]=str(app["host_port"]);env["HOST"]="127.0.0.1"
    env.update(private.get("environment",{}))
    # These values are part of the Termux execution environment, not app
    # settings; don't let deployment env silently break Android exec handling.
    for key in runtime_keys:
        if os.environ.get(key):env[key]=os.environ[key]
    env["PORT"]=str(app["host_port"]);env["HOST"]="127.0.0.1"
    env["MYFILE_INTERNAL_PORT"]=str(app["container_port"]);env["MYFILE_HOST_PORT"]=str(app["host_port"])
    adapter=_source_port_adapter(app_id)
    existing_options=env.get("NODE_OPTIONS","").strip()
    env["NODE_OPTIONS"]=(existing_options+" " if existing_options else "")+"--require="+str(adapter)
    env["HOME"]=str(source_directory(app_id))
    env["NODE_ENV"]="production"
    env["TMPDIR"]=str(source_directory(app_id)/".tmp")
    Path(env["TMPDIR"]).mkdir(parents=True,exist_ok=True)
    return env


def _spawn_source(app):
    tools=node_toolchain()
    if not tools["available"]:raise RuntimeError(tools["message"])
    ref=parse_source_reference(app.get("image_reference"))
    root=source_directory(app["id"])
    if not ref or not (root/"package.json").is_file():raise RuntimeError("Prepared Node.js source or package.json is missing. Use Redeploy to fetch it again.")
    package=json.loads((root/"package.json").read_text(encoding="utf-8"))
    start=package.get("scripts",{}).get("start") if isinstance(package,dict) else None
    if not isinstance(start,str) or not start.strip():raise RuntimeError("This source repository does not define an npm start script.")
    runtime="github:"+ref["owner"]+"/"+ref["repo"]+"@"+ref["branch"]
    npm=tools["npm"]
    logread,logwrite=os.pipe()
    logger=subprocess.Popen([sys.executable,str(Path(__file__).with_name("hosting_log_writer.py")),app["id"]],stdin=logread,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,start_new_session=True)
    os.close(logread)
    try:
        proc=subprocess.Popen([npm,"run","start"],cwd=str(root),env=_node_environment(app["id"],app),stdin=subprocess.DEVNULL,
                              stdout=logwrite,stderr=subprocess.STDOUT,close_fds=True,start_new_session=True)
    finally:os.close(logwrite)
    proc._myfile_logger=logger
    with _LOCK:_PROCESSES[app["id"]]=proc
    ticks=_ticks(proc.pid)
    app_db.update_hosted_app(app["id"],OWNER,process_pid=proc.pid,process_start_ticks=ticks,status="starting",error="")
    _append_log(app["id"],f"Started ARM64 Termux Node.js source process pid={proc.pid} for {runtime} using {tools['version']}.")
    return proc


def _configure_oci_listener(app):
    """Adapt the standard Nginx default site to its allocated host port."""
    if app["container_port"]==app["host_port"]:return False
    root=_runtime_storage()/"containers"/app["runtime_alias"]/"rootfs"
    config_dir=root/"etc"/"nginx"/"conf.d"
    try:files=list(config_dir.glob("*.conf"))
    except OSError:return False
    changed=False
    for path in files:
        try:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):continue
            text=path.read_text(encoding="utf-8")
        except (OSError,UnicodeError):continue
        pattern=re.compile(r"(?m)^(\s*listen\s+)(\[::\]:)?"+re.escape(str(app["container_port"]))+r"(?=\s|;)")
        updated=pattern.sub(lambda m:m.group(1)+("[::1]:" if m.group(2) else "127.0.0.1:")+str(app["host_port"]),text)
        if updated!=text:
            path.write_text(updated,encoding="utf-8");changed=True
    if changed:_append_log(app["id"],f"Adjusted standard Nginx listener from container port {app['container_port']} to loopback host port {app['host_port']}.")
    return changed


def _spawn(app):
    if is_node_source(app): return _spawn_source(app)
    runtime = detect_runtime()
    if not runtime.available: raise RuntimeError("PRoot-Distro is unavailable: " + runtime.message)
    config = _manifest(app)
    command = _entrypoint(config, app)
    if not command: raise RuntimeError("Image has no entrypoint or command; configure an explicit command.")
    env_json = _SECRETS / (app["id"] + ".json")
    private_config = _private_config(app["id"])
    user_env=private_config.get("environment",{})
    if not app.get("command"): app=dict(app);app["command"]=private_config.get("command",[])
    all_env = _guest_env(config, user_env)
    all_env["PORT"]=str(app["host_port"])
    secret_path = _secret_path(app["id"])
    needs_wrapper = bool(user_env or app.get("command") or app["host_port"] != app["container_port"])
    if needs_wrapper:
        rootfs=_runtime_storage() / "containers" / app["runtime_alias"] / "rootfs"
        shell=rootfs / "bin" / "sh"
        # Inspect the guest directory entry without following an absolute guest symlink
        # through the Android host's filesystem namespace.
        shell_ok=shell.is_file() or shell.is_symlink()
        if not shell_ok: raise RuntimeError("This image has no usable /bin/sh; custom command or environment injection is unsupported.")
        _write_secrets(app["id"], all_env)
    backend = local_oci_backend()
    argv = backend.run_options()
    if needs_wrapper:
        argv.extend(["--bind", f"{secret_path}:/run/myfile-env"])
    workdir = config.get("WorkingDir")
    if isinstance(workdir, str) and workdir.startswith("/") and "\x00" not in workdir:
        argv.extend(["--work-dir", workdir])
    argv.append(backend.validate_alias(app["runtime_alias"]))
    if needs_wrapper:
        argv.extend(["--", "/bin/sh", "-c", 'set -a; . /run/myfile-env; exec "$@"', "myfile", *command])
    _append_log(app["id"], f"Launching OCI image {app['image_reference']} with PRoot-Distro alias {app['runtime_alias']}.")
    read_fd,write_fd=os.pipe()
    logger=subprocess.Popen([sys.executable,str(Path(__file__).with_name("hosting_log_writer.py")),app["id"]],stdin=read_fd,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,start_new_session=True)
    os.close(read_fd)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=write_fd, stderr=subprocess.STDOUT,
                                close_fds=True,start_new_session=True)
    finally: os.close(write_fd)
    proc._myfile_logger=logger
    with _LOCK: _PROCESSES[app["id"]] = proc
    ticks = _ticks(proc.pid)
    app_db.update_hosted_app(app["id"], OWNER, process_pid=proc.pid, process_start_ticks=ticks, status="starting", error="")
    _append_log(app["id"], f"Started PRoot-Distro process pid={proc.pid}; image stdout/stderr is captured below.")
    return proc


def _http_status(port, path, timeout=0.6):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        response = conn.getresponse(); status=response.status
        response.read(2048)
        return status
    finally: conn.close()


def _wait_ready(app_id, timeout=MAX_START_SECONDS):
    deadline = time.monotonic() + timeout
    last_error = "No HTTP response from configured port."
    while time.monotonic() < deadline:
        app = app_db.get_hosted_app(app_id, OWNER)
        if not app: raise RuntimeError("Application record disappeared during startup.")
        proc = _process(app_id)
        if proc and proc.poll() is not None:
            raise RuntimeError(f"Application process exited with code {proc.returncode} before readiness.")
        try:
            status = _http_status(app["host_port"], app["health_path"])
            if 200 <= status < 400:
                app_db.update_hosted_app(app_id, OWNER, status="running", error="")
                _append_log(app_id, f"Readiness succeeded with HTTP {status} on 127.0.0.1:{app['host_port']}.")
                return
            last_error = f"Health endpoint returned HTTP {status}."
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            last_error = f"Waiting for local HTTP readiness ({type(exc).__name__})."
        time.sleep(0.25)
    raise RuntimeError(f"Application did not pass readiness within {timeout} seconds: {last_error}")


def _image_size(alias):
    root = _runtime_storage() / "containers" / alias / "rootfs"
    if not root.exists(): return None
    total=0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base,d))]
        for name in files:
            file_path=os.path.join(base,name)
            try:
                if not os.path.islink(file_path):total += os.path.getsize(file_path)
            except OSError: pass
    return total


def _run_job(app_id, spec):
    app_db.update_hosted_app(app_id, OWNER, status="pulling", error="")
    _append_log(app_id, f"Image preparation started for {spec['image']} on {os.uname().machine}.")
    alias = _runtime_alias(app_id)
    pull_complete=False
    try:
        with _INSTALL_LOCK:
            free = shutil.disk_usage(_runtime_storage()).free
            if free < MIN_FREE_BYTES: raise RuntimeError(f"Only {free} bytes are free in PRoot-Distro storage; at least {MIN_FREE_BYTES} bytes are required before an image pull.")
            backend=local_oci_backend()
            try:
                manifest_info=inspect_public_docker_hub_image(spec["image"], os.uname().machine)
            except UnsupportedImage:
                raise
            except RegistryUnavailable as lookup_error:
                # Keep PRoot-Distro as the pull authority and preserve offline/cache
                # behavior when the advisory registry request is unavailable.
                _append_log(app_id, "Docker Hub preflight unavailable; deferring image resolution to PRoot-Distro: " + str(lookup_error))
            else:
                if manifest_info:
                    platforms=", ".join(sorted({f"{p['os']}/{p['architecture']}" + (f"/{p['variant']}" if p.get("variant") else "") for p in manifest_info["platforms"]}))
                    _append_log(app_id, f"Docker Hub manifest resolved before pull: {manifest_info['repository']}{manifest_info['selector']}; native architecture {manifest_info['architecture']}; published platforms: {platforms}.")
                else:
                    _append_log(app_id, "Image registry is not Docker Hub; architecture preflight is delegated to PRoot-Distro.")
            proc = subprocess.Popen(backend.install_argv(alias, spec["image"]),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, close_fds=True, start_new_session=True)
            def capture_pull():
                try:
                    if proc.stdout:
                        for line in proc.stdout:
                            _append_log(app_id,line.rstrip())
                            with _LOCK: _JOBS[app_id]["lastOutput"] = _redact_line(app_id,line.rstrip())[-300:]
                except OSError: pass
            reader=threading.Thread(target=capture_pull,name=f"myfile-pull-log-{app_id[:8]}",daemon=True); reader.start()
            try: code = proc.wait(timeout=MAX_INSTALL_SECONDS)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid,signal.SIGKILL)
                except OSError: proc.kill()
                proc.wait(timeout=5); reader.join(timeout=1)
                raise RuntimeError(f"Image pull/installation exceeded {MAX_INSTALL_SECONDS} seconds.")
            reader.join(timeout=2)
            if code: raise RuntimeError(f"PRoot-Distro could not prepare image (exit {code}); review this app's logs.")
            pull_complete=True
        size = _image_size(alias)
        if size is None: raise RuntimeError("Runtime completed image installation but its root filesystem is missing.")
        if size > MAX_IMAGE_ROOT_BYTES:
            subprocess.run(local_oci_backend().remove_argv(alias),capture_output=True,timeout=60)
            raise RuntimeError(f"Unpacked image uses {size} bytes; configured per-image limit is {MAX_IMAGE_ROOT_BYTES}. The test image was removed.")
        with _LOCK: _JOBS[app_id].update(progress="image-ready", imageBytes=size)
        with _app_lock(app_id):
            latest=app_db.get_hosted_app(app_id,OWNER)
            if not latest or latest["status"]!="pulling":return
            latest=_resolve_image_port(latest)
            app_db.update_hosted_app(app_id, OWNER, status="starting", error="")
            app=app_db.get_hosted_app(app_id,OWNER)
            _configure_oci_listener(app)
            app["command"]=_private_config(app_id).get("command",[])
            _spawn(app)
            _wait_ready(app_id)
            with _LOCK: _JOBS[app_id].update(status="complete", progress="ready", finishedAt=time.time())
    except Exception as exc:
        if not pull_complete and (_runtime_storage()/"containers"/alias).exists():
            try:subprocess.run(local_oci_backend().remove_argv(alias),capture_output=True,timeout=60)
            except Exception:pass
        message = str(exc)[:500]
        _append_log(app_id, "Deployment failed: " + message)
        current = app_db.get_hosted_app(app_id, OWNER)
        if current and current.get("process_pid") and _owned_process_alive(current):
            proc=_process(app_id)
            try:
                if proc and proc.poll() is None:
                    proc.send_signal(signal.SIGQUIT)
                    try: proc.wait(timeout=8)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)
                    with _LOCK: _PROCESSES.pop(app_id,None)
                else: os.kill(current["process_pid"], signal.SIGQUIT)
            except OSError: pass
        app_db.update_hosted_app(app_id, OWNER, status="failed", error=message)
        with _LOCK: _JOBS[app_id].update(status="failed", progress="failed", error=message, finishedAt=time.time())


def _run_node_source_job(app_id, spec):
    app_db.update_hosted_app(app_id,OWNER,status="pulling",error="")
    _append_log(app_id,f"Fetching GitHub Node.js source {spec['owner']}/{spec['repo']} branch {spec['branch']} on {os.uname().machine}.")
    tools=node_toolchain();source_root=source_directory(app_id);staging=_SOURCES/("."+app_id+".staging")
    try:
        if not tools["available"]:raise RuntimeError(tools["message"])
        if tools["architecture"] not in ("arm64","x64","arm"):raise RuntimeError(f"Termux Node.js architecture {tools['architecture']} is unsupported.")
        free=shutil.disk_usage(_STATE).free
        if free<MIN_SOURCE_FREE_BYTES:raise RuntimeError(f"Only {free} bytes are free for source deployment; at least {MIN_SOURCE_FREE_BYTES} bytes are required.")
        if staging.exists():shutil.rmtree(staging)
        if source_root.exists():shutil.rmtree(source_root)
        env=os.environ.copy();env["GIT_TERMINAL_PROMPT"]="0"
        git=shutil.which("git")
        if not git:raise RuntimeError("Git is required for GitHub source deployment.")
        url=f"https://github.com/{spec['owner']}/{spec['repo']}.git"
        with _INSTALL_LOCK:
            _run_source_command(app_id,[git,"clone","--quiet","--depth=1","--single-branch","--branch",spec["branch"],url,str(staging)],
                                _STATE,env,MAX_SOURCE_INSTALL_SECONDS,"GitHub source download")
            package_path=staging/"package.json";lock_path=staging/"package-lock.json"
            if not package_path.is_file() or not lock_path.is_file():raise RuntimeError("Node.js source deployment requires package.json and package-lock.json for a reproducible install.")
            try:package=json.loads(package_path.read_text(encoding="utf-8"))
            except (OSError,ValueError) as exc:raise RuntimeError("The repository package.json is invalid.") from exc
            if not isinstance(package,dict) or not isinstance(package.get("scripts"),dict) or not isinstance(package["scripts"].get("start"),str):
                raise RuntimeError("The repository must define an npm start script.")
            size=_source_tree_size(staging)
            if size>MAX_SOURCE_BYTES:raise RuntimeError(f"Downloaded source exceeds the {MAX_SOURCE_BYTES}-byte source deployment limit.")
            npm_env=dict(env);npm_env["npm_config_cache"]=str(_STATE/"npm-cache"/app_id)
            _run_source_command(app_id,[tools["npm"],"ci","--omit=dev","--no-audit","--no-fund","--ignore-scripts"],
                                staging,npm_env,MAX_SOURCE_INSTALL_SECONDS,"ARM64 npm dependency installation")
            size=_source_tree_size(staging)
            if size>MAX_SOURCE_BYTES:raise RuntimeError(f"Source and installed dependencies exceed the {MAX_SOURCE_BYTES}-byte deployment limit.")
            source_root.parent.mkdir(parents=True,exist_ok=True)
            os.replace(staging,source_root)
        with _LOCK:_JOBS[app_id].update(progress="source-ready",sourceBytes=size,nodeVersion=tools["version"])
        with _app_lock(app_id):
            latest=app_db.get_hosted_app(app_id,OWNER)
            if not latest or latest["status"]!="pulling":return
            app_db.update_hosted_app(app_id,OWNER,status="starting",error="")
            row=app_db.get_hosted_app(app_id,OWNER);row["command"]=[]
            _spawn(row);_wait_ready(app_id)
            with _LOCK:_JOBS[app_id].update(status="complete",progress="ready",finishedAt=time.time())
    except Exception as exc:
        if staging.exists():
            try:shutil.rmtree(staging)
            except OSError:pass
        message=str(exc)[:500];_append_log(app_id,"Source deployment failed: "+message)
        current=app_db.get_hosted_app(app_id,OWNER)
        if current and current.get("process_pid") and _owned_process_alive(current):
            proc=_process(app_id)
            try:
                if proc and proc.poll() is None:
                    try:os.killpg(proc.pid,signal.SIGTERM)
                    except OSError:proc.terminate()
                    try:proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=3)
                    with _LOCK:_PROCESSES.pop(app_id,None)
                else:os.kill(current["process_pid"],signal.SIGTERM)
            except OSError:pass
        app_db.update_hosted_app(app_id,OWNER,status="failed",error=message,process_pid=None,process_start_ticks=None)
        with _LOCK:_JOBS[app_id].update(status="failed",progress="failed",error=message,finishedAt=time.time())


def create_node_source(data):
    spec=_validate_source_spec(data);tools=node_toolchain()
    if not tools["available"]:raise RuntimeError(tools["message"])
    host_arch=os.uname().machine.lower();node_arch=tools["architecture"]
    arch_compatible={"aarch64":{"arm64"},"arm64":{"arm64"},"x86_64":{"x64"},"amd64":{"x64"},"armv7l":{"arm"}}
    if node_arch not in arch_compatible.get(host_arch,{node_arch}):
        raise RuntimeError(f"Node.js architecture {node_arch} does not match device architecture {host_arch}.")
    app_db.init();_dirs()
    with _CONTROL_LOCK:
        current_rows=app_db.list_hosted_apps(OWNER)
        for row in current_rows:
            if row["status"] in ("running","starting"):_alive_status(row)
        current_rows=app_db.list_hosted_apps(OWNER)
        spec["hostPort"]=_allocate_host_port(current_rows)
        app_id=uuid.uuid4().hex;log_file=str(_log_path(app_id));source_dir=source_directory(app_id)
        private={"environment":spec["environment"],"command":[],"source":{"owner":spec["owner"],"repo":spec["repo"],"branch":spec["branch"]}}
        private_path=_SECRETS/(app_id+".json");private_path.write_text(json.dumps(private,ensure_ascii=False),encoding="utf-8");os.chmod(private_path,0o600)
        try:
            row=app_db.create_hosted_app({"id":app_id,"owner_id":OWNER,"name":spec["name"],"image_reference":spec["image"],
                "runtime_alias":_source_alias(app_id),"container_port":spec["containerPort"],"host_port":spec["hostPort"],"health_path":spec["healthPath"],
                "public":spec["public"],"command":[],"environmentKeys":sorted(spec["environment"]),"status":"queued","log_file":log_file})
        except Exception:
            try:private_path.unlink()
            except OSError:pass
            raise
        with _LOCK:_JOBS[app_id]={"id":app_id,"status":"queued","progress":"queued","createdAt":time.time(),"lastOutput":""}
        thread=threading.Thread(target=_run_node_source_job,args=(app_id,spec),name=f"myfile-source-{app_id[:8]}",daemon=True);thread.start()
    return public_app(row)


def create_application(data):
    spec = _validate_spec(data)
    runtime = detect_runtime()
    if not runtime.available: raise RuntimeError("Runtime unavailable: " + runtime.message)
    app_db.init(); _dirs()
    with _CONTROL_LOCK:
        # Reconcile stale state; concurrent apps are limited by real ports and resources.
        current_rows=app_db.list_hosted_apps(OWNER)
        for row in current_rows:
            if row["status"] in ("running","starting"): _alive_status(row)
        current_rows=app_db.list_hosted_apps(OWNER)
        spec["hostPort"]=_allocate_host_port(current_rows)
        app_id = uuid.uuid4().hex
        log_file = str(_log_path(app_id))
        overrides_path = _SECRETS / (app_id + ".json")
        overrides_path.write_text(json.dumps({"environment":spec["environment"],"command":spec["command"]}, ensure_ascii=False), encoding="utf-8")
        os.chmod(overrides_path,0o600)
        try:
            row=app_db.create_hosted_app({"id":app_id,"owner_id":OWNER,"name":spec["name"],"image_reference":spec["image"],
              "runtime_alias":_runtime_alias(app_id),"container_port":spec["containerPort"],"host_port":spec["hostPort"],
              "health_path":spec["healthPath"],"public":spec["public"],"command":[],
              "environmentKeys":sorted(spec["environment"]),"status":"queued","log_file":log_file})
        except Exception:
            try:overrides_path.unlink()
            except OSError:pass
            raise
        with _LOCK: _JOBS[app_id]={"id":app_id,"status":"queued","progress":"queued","createdAt":time.time(),"lastOutput":""}
        thread=threading.Thread(target=_run_job,args=(app_id,spec),name=f"myfile-deploy-{app_id[:8]}",daemon=True)
        thread.start()
    return public_app(row)


def _deployment_runtime_details(app):
    host = os.uname().machine.lower()
    native = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64", "armv7l": "arm"}.get(host, host)
    details = {"runtimeBackend": "Termux Node.js" if is_node_source(app) else "PRoot-Distro OCI",
               "executionMode": "Direct ARM Termux process; not container-isolated" if is_node_source(app) else "PRoot user-space execution; not Docker Engine",
               "hostArchitecture": native, "imageArchitecture": None, "architectureCompatible": None,
               "imageConfiguration": None, "unsupportedRequirements": []}
    if is_node_source(app):
        details["imageArchitecture"] = native
        details["architectureCompatible"] = True
        return details
    manifest_path = _runtime_storage() / "containers" / app["runtime_alias"] / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        image_config = manifest.get("image_config", {})
        config = image_config.get("config", {}) if isinstance(image_config, dict) else {}
        if not isinstance(config, dict): config = {}
        image_arch = str(image_config.get("architecture", manifest.get("arch", ""))).lower()
        details["imageArchitecture"] = image_arch or None
        details["architectureCompatible"] = architecture_matches(image_arch, host)
        details["imageConfiguration"] = {
            "workingDirectory": config.get("WorkingDir") if isinstance(config.get("WorkingDir"), str) else None,
            "hasEntrypoint": bool(config.get("Entrypoint")),
            "hasDefaultCommand": bool(config.get("Cmd")),
            "exposedPorts": sorted(config.get("ExposedPorts", {}).keys()) if isinstance(config.get("ExposedPorts"), dict) else [],
            "environmentKeys": sorted({item.split("=", 1)[0] for item in config.get("Env", []) if isinstance(item, str) and "=" in item}),
            "dockerHealthcheckConfigured": isinstance(config.get("Healthcheck"), dict),
            "volumeDeclarations": len(config.get("Volumes", {})) if isinstance(config.get("Volumes"), dict) else 0,
        }
        if image_arch and not details["architectureCompatible"]:
            qemu = shutil.which("qemu-x86_64-static") or shutil.which("qemu-x86_64")
            message = f"Image architecture {image_arch} does not match device architecture {native}."
            details["unsupportedRequirements"].append(message + (" A QEMU user-mode binary exists but is not integrated; use an ARM64/multi-platform image." if qemu else " No QEMU user-mode emulator is installed; use an ARM64/multi-platform image."))
        if isinstance(config.get("Healthcheck"), dict):
            details["unsupportedRequirements"].append("Image Docker HEALTHCHECK is not run; readiness uses the configured HTTP health path.")
        if config.get("Volumes"):
            details["unsupportedRequirements"].append("Image VOLUME declarations are not backed by persistent host volumes; container files remain in its private rootfs and are removed with that deployment.")
        if int(app.get("container_port") or 0) and app.get("container_port") != app.get("host_port"):
            details["unsupportedRequirements"].append("PRoot has no Docker port mapping. The image must honor the injected PORT value or be specifically adapted to listen on its assigned host port; the controller verifies HTTP readiness there.")
    except (OSError, ValueError, TypeError):
        details["imageConfiguration"] = None
        details["unsupportedRequirements"].append("Image manifest/configuration metadata is unavailable.")
    details["unsupportedRequirements"].append("PRoot does not provide kernel namespace isolation or enforce CPU/RAM limits; deploy only trusted images.")
    return details


def public_app(app, include_log=False):
    if not app:return None
    result={k:app[k] for k in ("id","name","image_reference","host_port","health_path","status","public","error","created_at","updated_at")}
    result["container_port"] = app["container_port"] or None
    result["route"] = f"/hosted/{app['id']}/"
    result["environmentKeys"] = app["environmentKeys"]
    result["running"] = _alive_status(app)
    if not result["running"] and app["status"] in ("running","starting"):
        if app["status"]=="starting" and not app.get("process_pid"):
            result["status"]="starting"
            result["error"]=""
        else:
            result["status"]="stopped"
            if not result["error"]:result["error"]="Managed process is not running."
    result["publicUrl"] = result["route"] if app["public"] else None
    result["deploymentType"] = "node-source" if is_node_source(app) else "oci-image"
    result.update(_deployment_runtime_details(app))
    source=parse_source_reference(app.get("image_reference")) if is_node_source(app) else None
    if source:result["source"]={"provider":"GitHub","owner":source["owner"],"repository":source["repo"],"branch":source["branch"]}
    if include_log: result["logs"] = _tail(app["log_file"])
    with _LOCK:
        job=_JOBS.get(app["id"])
        result["job"] = dict(job) if job else None
    return result


def _alive_status(app):
    proc=_process(app["id"])
    if proc is not None:
        if proc.poll() is None:return True
        with _LOCK: _PROCESSES.pop(app["id"],None)
        app_db.update_hosted_app(app["id"],OWNER,status="stopped",process_pid=None,process_start_ticks=None,error=f"Application exited with code {proc.returncode}.")
        return False
    if app["status"] in ("running","starting") and _owned_process_alive(app): return True
    if app["status"] == "starting" and not app.get("process_pid"):
        # A start worker may have been scheduled but not yet spawned; do not
        # mistake that short transition window for a crashed application.
        return False
    if app["status"] in ("running","starting"):
        app_db.update_hosted_app(app["id"],OWNER,status="stopped",process_pid=None,process_start_ticks=None,error="Managed runtime process is no longer available.")
    return False


def list_applications(include_logs=False):
    return [public_app(app,include_logs) for app in app_db.list_hosted_apps(OWNER)]


def get_application(app_id, include_logs=False):
    if not APP_ID_RE.fullmatch(str(app_id)): return None
    return public_app(app_db.get_hosted_app(app_id,OWNER),include_logs)


@_serialized_per_app
def start_application(app_id):
    with _CONTROL_LOCK:
        app=app_db.get_hosted_app(app_id,OWNER)
        if not app: return None
        if app["status"] == "running" and _alive_status(app): return public_app(app_db.get_hosted_app(app_id,OWNER))
        if app["status"] in ("queued","pulling","starting","stopping"):
            raise RuntimeError(f"This deployment is already {app['status']}; wait for that operation to finish.")
        rows=app_db.list_hosted_apps(OWNER)
        _allocate_host_port(rows,requested=app["host_port"],exclude_id=app_id)
        if is_node_source(app):
            if not (source_directory(app_id)/"package.json").is_file():raise RuntimeError("The prepared Node.js source is missing. Use Redeploy to fetch it again.")
            if not node_toolchain()["available"]:raise RuntimeError("Termux Node.js/npm is unavailable; install the Android ARM64 Termux nodejs package first.")
        elif not (_runtime_storage()/"containers"/app["runtime_alias"]/"manifest.json").is_file():
            raise RuntimeError("The installed image is missing. Use redeploy after checking available storage.")
        app_db.update_hosted_app(app_id,OWNER,status="starting",error="")
        with _LOCK: _JOBS[app_id]={"id":app_id,"status":"starting","progress":"starting","createdAt":time.time(),"lastOutput":""}
        thread=threading.Thread(target=_start_existing,args=(app_id,),name=f"myfile-start-{app_id[:8]}",daemon=True)
        thread.start()
        return public_app(app_db.get_hosted_app(app_id,OWNER))


@_serialized_per_app
def _start_existing(app_id):
    try:
        app=app_db.get_hosted_app(app_id,OWNER)
        if not app or app["status"]!="starting":return
        app["command"]=_private_config(app_id).get("command",[])
        app_db.update_hosted_app(app_id,OWNER,status="starting",error="",process_pid=None,process_start_ticks=None)
        _spawn(app); _wait_ready(app_id)
        with _LOCK: _JOBS[app_id].update(status="complete",progress="ready",finishedAt=time.time())
    except Exception as exc:
        app_db.update_hosted_app(app_id,OWNER,status="failed",error=str(exc)[:500])
        _append_log(app_id,"Start failed: "+str(exc)[:500])
        with _LOCK:
            if app_id in _JOBS:_JOBS[app_id].update(status="failed",progress="failed",error=str(exc)[:500])


@_serialized_per_app
def stop_application(app_id):
    app=app_db.get_hosted_app(app_id,OWNER)
    if not app:return None
    if app["status"] in ("queued","pulling"):
        raise RuntimeError("Image preparation cannot be cancelled safely. Wait for it to finish, then stop or delete the app.")
    app_db.update_hosted_app(app_id,OWNER,status="stopping",error="")
    proc=_process(app_id)
    if proc is not None and proc.poll() is None:
        if is_node_source(app):
            try:os.killpg(proc.pid,signal.SIGTERM)
            except OSError:proc.terminate()
        else:proc.send_signal(signal.SIGQUIT)
        try: code=proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if is_node_source(app):
                try:os.killpg(proc.pid,signal.SIGKILL)
                except OSError:proc.kill()
            else:proc.kill()
            code=proc.wait(timeout=3)
        try: proc._myfile_logger.wait(timeout=3)
        except (AttributeError,subprocess.TimeoutExpired): pass
        with _LOCK: _PROCESSES.pop(app_id,None)
    elif _owned_process_alive(app):
        os.kill(app["process_pid"],signal.SIGTERM if is_node_source(app) else signal.SIGQUIT)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and _ticks(app["process_pid"])==app["process_start_ticks"]: time.sleep(.1)
        if _ticks(app["process_pid"])==app["process_start_ticks"]: os.kill(app["process_pid"],signal.SIGKILL)
    app_db.update_hosted_app(app_id,OWNER,status="stopped",process_pid=None,process_start_ticks=None,error="")
    _append_log(app_id,"Application stopped by administrator.")
    return public_app(app_db.get_hosted_app(app_id,OWNER))


@_serialized_per_app
def restart_application(app_id):
    stopped=stop_application(app_id)
    if not stopped:return None
    return start_application(app_id)


@_serialized_per_app
def delete_application(app_id):
    app=app_db.get_hosted_app(app_id,OWNER)
    if not app:return False
    if app["status"] in ("queued","pulling"):
        raise RuntimeError("Image preparation cannot be cancelled safely. Wait for it to finish, then delete the app.")
    stop_application(app_id)
    alias=app["runtime_alias"]
    if is_node_source(app):
        source_dir=source_directory(app_id)
        if source_dir.exists():shutil.rmtree(source_dir)
    else:
        result=subprocess.run(local_oci_backend().remove_argv(alias),capture_output=True,text=True,timeout=120) if (_runtime_storage()/"containers"/alias).exists() else subprocess.CompletedProcess([],0,"","")
        if result.returncode:
            raise RuntimeError("App process stopped, but its PRoot-Distro image could not be removed: "+result.stderr[-500:])
    for path in (_secret_path(app_id),_SECRETS/(app_id+".json"),_log_path(app_id),_log_path(app_id).with_suffix(".log.1")):
        try:path.unlink()
        except FileNotFoundError:pass
    if is_node_source(app):
        cache=_STATE/"npm-cache"/app_id
        if cache.exists():shutil.rmtree(cache)
    app_db.delete_hosted_app(app_id,OWNER)
    return True


def app_logs(app_id):
    app=app_db.get_hosted_app(app_id,OWNER)
    if not app:return None
    return {"appId":app_id,"logs":_tail(app["log_file"]),"updatedAt":app["updated_at"]}


@_serialized_per_app
def redeploy_application(app_id):
    with _CONTROL_LOCK:
        app=app_db.get_hosted_app(app_id,OWNER)
        if not app:return None
        stop_application(app_id)
        source_app=is_node_source(app)
        if source_app:
            source_dir=source_directory(app_id)
            if source_dir.exists():shutil.rmtree(source_dir)
            private=_private_config(app_id);source=private.get("source",{})
            spec={"name":app["name"],"owner":source.get("owner"),"repo":source.get("repo"),"branch":source.get("branch"),
                  "image":app["image_reference"],"containerPort":app["container_port"],"hostPort":app["host_port"],"healthPath":app["health_path"],
                  "environment":private.get("environment",{}),"public":app["public"]}
        else:
            alias=app["runtime_alias"]
            result=subprocess.run(local_oci_backend().remove_argv(alias),capture_output=True,text=True,timeout=120)
            if result.returncode and (_runtime_storage()/"containers"/alias).exists():
                raise RuntimeError("Could not remove the previous image before redeploying: "+result.stderr[-400:])
            spec={"image":app["image_reference"]}
        app_db.update_hosted_app(app_id,OWNER,status="queued",error="",process_pid=None,process_start_ticks=None)
        with _LOCK: _JOBS[app_id]={"id":app_id,"status":"queued","progress":"queued","createdAt":time.time(),"lastOutput":""}
        worker=_run_node_source_job if source_app else _run_job
        thread=threading.Thread(target=worker,args=(app_id,spec),name=f"myfile-redeploy-{app_id[:8]}")
        thread.start()
        return public_app(app_db.get_hosted_app(app_id,OWNER))


def recover_applications():
    """Reattach to surviving deployments or restart stale active rows once per server process."""
    global _RECOVERY_DONE
    with _RECOVERY_LOCK:
        if _RECOVERY_DONE:return []
        _RECOVERY_DONE=True
        app_db.init();_dirs();recovered=[]
        for app in app_db.list_hosted_apps(OWNER):
            if app.get("status") not in ("running","starting"):continue
            if _owned_process_alive(app):
                continue
            app_id=app["id"]
            _append_log(app_id,"Recovering previously active application after MyFile server startup.")
            app_db.update_hosted_app(app_id,OWNER,status="stopped",process_pid=None,process_start_ticks=None,
                                    error="The previous runtime process was unavailable during MyFile startup; attempting recovery.")
            try:
                result=start_application(app_id)
                if result:recovered.append(app_id)
            except Exception as exc:
                message=("Automatic recovery failed: "+str(exc))[:500]
                app_db.update_hosted_app(app_id,OWNER,status="failed",error=message)
                _append_log(app_id,message)
        return recovered


def runtime_status():
    runtime=detect_runtime()
    for app in app_db.list_hosted_apps(OWNER):
        if app["status"] in ("queued","pulling"):
            with _LOCK: tracked=app["id"] in _JOBS
            if not tracked:
                app_db.update_hosted_app(app["id"],OWNER,status="failed",error="Image preparation was interrupted by a MyFile server restart. Retry with Redeploy.")
    try: free=shutil.disk_usage(_runtime_storage()).free
    except OSError: free=None
    resources=_resource_snapshot()
    if free is not None and free<LOW_STORAGE_WARNING_BYTES and "Hosting storage has less than 1 GiB free; image pulls may fail." not in resources["warnings"]:
        resources["warnings"].append("PRoot image storage has less than 1 GiB free; image pulls may fail.")
    return {"available":runtime.available,"runtime":runtime.as_dict(),"backends":runtime_inventory().get("backends",[]),"limitations":runtime_inventory().get("limitations",[]),"nodeSource":node_toolchain(),"storageFreeBytes":free,
            "resources":resources,"security":"PRoot is not isolation and Node source runs directly as the Termux user; deploy only trusted administrator-selected applications.",
            "publicRouting":"Stable authenticated-server path /hosted/<id>/; external reachability has not been tested."}


def deployment_job(app_id):
    with _LOCK: job=_JOBS.get(app_id)
    app=get_application(app_id)
    if not app:return None
    return {"app":app,"job":dict(job) if job else None}


def _route_app(path):
    match=PUBLIC_ROUTE_RE.fullmatch(urllib.parse.urlsplit(path).path)
    return app_db.get_hosted_app(match.group(1)) if match else None


def is_hosted_path(path):
    return PUBLIC_ROUTE_RE.fullmatch(urllib.parse.urlsplit(path).path) is not None


def proxy_request(handler):
    """Proxy only a registered app port; only explicitly published apps bypass auth."""
    app=_route_app(handler.path)
    if app is None:
        handler.reply(404,b"Hosted application not found"); return
    if not app["public"] and not handler.authenticate(): return
    if app["status"] != "running" or not _owned_process_alive(app):
        handler.reply(503,b"Hosted application is stopped or unavailable"); return
    split=urllib.parse.urlsplit(handler.path)
    prefix=f"/hosted/{app['id']}"
    target=split.path[len(prefix):] or "/"
    if not target.startswith("/") or "\r" in target or "\n" in target:
        handler.reply(400,b"Invalid hosted route"); return
    target += ("?"+split.query) if split.query else ""
    length=0
    try:length=int(handler.headers.get("Content-Length","0"))
    except ValueError:handler.reply(400,b"Invalid content length");return
    if handler.headers.get("Transfer-Encoding"):
        handler.reply(501,b"Chunked request forwarding is not supported"); return
    if length<0 or length>10*1024*1024:handler.reply(413,b"Request body too large");return
    body=handler.rfile.read(length) if length else None
    headers={}
    for key in ("Accept","Accept-Encoding","Content-Type","Content-Length","User-Agent","Range","If-None-Match","If-Modified-Since","Origin","Cookie"):
        value=handler.headers.get(key)
        if value is not None:headers[key]=value
    headers["Host"]=handler.headers.get("Host",f"127.0.0.1:{app['host_port']}")
    headers["X-Forwarded-Host"]=headers["Host"]
    headers["X-Forwarded-Proto"]="https" if handler.client_address and handler.server.server_port == 443 else "http"
    headers["X-Forwarded-For"]=handler.client_address[0] if handler.client_address else "127.0.0.1"
    headers["X-Forwarded-Prefix"]=prefix
    conn=http.client.HTTPConnection("127.0.0.1",app["host_port"],timeout=15)
    try:
        conn.request(handler.command,target,body=body,headers=headers)
        upstream=conn.getresponse()
        handler.send_response(upstream.status,upstream.reason)
        hop={"connection","keep-alive","proxy-authenticate","proxy-authorization","te","trailers","transfer-encoding","upgrade"}
        for key,value in upstream.getheaders():
            if key.lower() not in hop and key.lower() not in {"content-length","server","date"}:
                handler.send_header(key,value)
        if upstream.length is None:
            handler.protocol_version="HTTP/1.1"
            handler.send_header("Transfer-Encoding","chunked")
        else:
            handler.send_header("Content-Length",str(upstream.length))
        handler.send_header("X-Content-Type-Options","nosniff")
        handler.end_headers()
        if handler.command != "HEAD":
            while True:
                chunk=upstream.read(65536)
                if not chunk:break
                if upstream.length is None:handler.wfile.write(f"{len(chunk):X}\r\n".encode()+chunk+b"\r\n")
                else:handler.wfile.write(chunk)
            if upstream.length is None:handler.wfile.write(b"0\r\n\r\n")
    except (OSError,TimeoutError,http.client.HTTPException,BrokenPipeError,ConnectionResetError):
        if not handler.wfile.closed:
            try:handler.close_connection=True
            except Exception:pass
    finally:conn.close()
