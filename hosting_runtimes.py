"""Small runtime adapter and capability inventory for hosted deployments.

Only PRoot-Distro OCI execution and direct Termux Node.js source execution are
implemented locally. Docker/Rootless Docker/remote Docker are detected as
capabilities; the inventory never implies that an unavailable backend ran an app.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from execution_engine.runtime import detect_runtime


@dataclass(frozen=True)
class ProotOCIBackend:
    """Safe argv builder for the already-installed PRoot-Distro executable."""
    executable: str
    name: str = "proot-oci"
    display_name: str = "PRoot-Distro OCI (user-space, not Docker Engine)"

    @staticmethod
    def native_architecture(machine: str | None = None) -> str:
        value = (machine or os.uname().machine).lower()
        mapping = {"aarch64": "aarch64", "arm64": "aarch64", "armv7l": "arm",
                   "x86_64": "x86_64", "amd64": "x86_64", "i686": "i686"}
        try:
            return mapping[value]
        except KeyError as exc:
            raise RuntimeError(f"PRoot-Distro image execution is unsupported for host architecture {value}.") from exc

    def install_argv(self, alias: str, image: str, machine: str | None = None) -> list[str]:
        if not alias or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in alias):
            raise ValueError("Invalid runtime alias.")
        if not image or "\x00" in image:
            raise ValueError("Invalid OCI image reference.")
        return [self.executable, "install", "--quiet", "--name", alias,
                "--architecture", self.native_architecture(machine), image]

    def run_options(self) -> list[str]:
        return [self.executable, "run", "--isolated", "--redirect-ports"]

    @staticmethod
    def validate_alias(alias: str) -> str:
        if not alias or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in alias):
            raise ValueError("Invalid runtime alias.")
        return alias

    def remove_argv(self, alias: str) -> list[str]:
        if not alias or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in alias):
            raise ValueError("Invalid runtime alias.")
        return [self.executable, "remove", alias]


_CACHE_LOCK = threading.Lock()
_CACHE: tuple[float, dict] | None = None


def _namespace_probe(kind: str) -> bool | None:
    """Run a fixed, non-mutating unshare probe without forking Python ourselves."""
    helper = shutil.which("unshare")
    if not helper:
        return None
    if kind == "user": args = [helper, "-Ur", "true"]
    elif kind == "mount": args = [helper, "-m", "true"]
    else: raise ValueError("Unknown namespace probe.")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3,
                                env={"PATH": os.environ.get("PATH", ""), "LANG": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def runtime_inventory(force: bool = False) -> dict:
    """Return bounded, secret-free local runtime capability diagnostics."""
    global _CACHE
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE and now - _CACHE[0] < 60:
            return dict(_CACHE[1])
    proot = detect_runtime()
    machine = os.uname().machine.lower()
    native = machine in ("aarch64", "arm64")
    docker = shutil.which("docker")
    engine_binaries = {name: shutil.which(name) for name in ("dockerd", "containerd", "runc")}
    docker_daemon = {"status": "NOT AVAILABLE", "message": "Docker Engine client is not installed."}
    if docker:
        try:
            # Pin to the conventional local socket; ignore any remote DOCKER_HOST/context.
            probe = subprocess.run([docker, "--host", "unix:///var/run/docker.sock", "info", "--format", "{{.ServerVersion}}"],
                                   capture_output=True, text=True, timeout=3,
                                   env={"PATH": os.environ.get("PATH", ""), "LANG": "C"})
            docker_daemon = ({"status": "AVAILABLE", "version": probe.stdout.strip()[:80], "message": "Local Docker Engine responded."}
                             if probe.returncode == 0 else {"status": "NOT AVAILABLE", "message": "Docker CLI exists, but no functional Engine answered on the local socket."})
        except (OSError, subprocess.TimeoutExpired):
            docker_daemon = {"status": "NOT AVAILABLE", "message": "Docker Engine probe failed or timed out."}
    userns = _namespace_probe("user")
    mountns = _namespace_probe("mount")
    cgroup2 = Path("/sys/fs/cgroup/cgroup.controllers")
    try:
        controllers = cgroup2.read_text(encoding="ascii").split()
        cgroup_ready = bool(controllers)
    except OSError:
        controllers, cgroup_ready = [], False
    rootless_bins = {name: shutil.which(name) is not None for name in ("rootlesskit", "slirp4netns", "newuidmap", "newgidmap", "fuse-overlayfs")}
    qemu = {arch: (shutil.which(f"qemu-{arch}-static") or shutil.which(f"qemu-{arch}")) for arch in ("x86_64", "i386", "arm")}
    engine_ready = docker_daemon["status"] == "AVAILABLE"
    rootless_ready = bool(engine_ready and userns and mountns and cgroup_ready and all(rootless_bins.values()))
    result = {
        "host": {"os": "Android/Termux" if os.environ.get("PREFIX", "").startswith("/data/data/com.termux/") else "Linux", "architecture": machine},
        "backends": [
            {"id": "docker-engine", "name": "Docker Engine", "status": "AVAILABLE" if engine_ready else "UNAVAILABLE", "details": docker_daemon["message"], "clientInstalled": bool(docker), "engineBinaries": {name: bool(path) for name, path in engine_binaries.items()}, "resourceLimits": engine_ready},
            {"id": "rootless-docker", "name": "Rootless Docker", "status": "AVAILABLE" if rootless_ready else "UNAVAILABLE", "details": "Rootless prerequisites are present." if rootless_ready else "Requires a working Docker daemon, user/mount namespaces, delegated cgroups, rootlesskit, slirp4netns, uidmap helpers, and fuse-overlayfs.", "userNamespaces": userns, "mountNamespaces": mountns, "cgroupV2": cgroup_ready, "cgroupControllers": controllers, "helpers": rootless_bins},
            {"id": "proot-oci", "name": "PRoot-Distro OCI", "status": "AVAILABLE" if proot.available else "UNAVAILABLE", "details": "OCI registry pull and entrypoint execution are available for tested compatible images. Native architecture is preferred; QEMU must be installed and integrated explicitly. This is not Docker Engine and provides no kernel isolation or cgroup limits.", "version": proot.version, "nativeArchitecture": ProotOCIBackend.native_architecture(machine) if native else machine, "qemuUserEmulators": qemu},
            {"id": "termux-node-source", "name": "Termux Node.js source", "status": "AVAILABLE" if shutil.which("node") and shutil.which("npm") else "UNAVAILABLE", "details": "Separate GitHub-source workflow; this is not Docker image execution."},
            {"id": "remote-docker", "name": "Remote Docker Engine", "status": "NOT CONFIGURED", "details": "No authenticated TLS/SSH remote engine is configured; no deployment is sent remotely."},
        ],
        "limitations": ["PRoot is not a kernel container and does not isolate processes, networking, or filesystem using namespaces.",
                        "This controller does not configure Docker VOLUME persistence, arbitrary mounts, privileged mode, Docker networks, Docker HEALTHCHECK execution, or enforced CPU/RAM limits.",
                        "Only HTTP readiness checks on the configured application path determine readiness; Docker HEALTHCHECK is not run.",
                        "PRoot does not provide Docker port mappings or network namespaces. An image must listen on the assigned host port (or honor PORT), or be specifically adapted; public proxying supports HTTP only.",
                        "The current controller does not configure QEMU emulation."],
    }
    with _CACHE_LOCK:
        _CACHE = (now, result)
    return dict(result)


def local_oci_backend() -> ProotOCIBackend:
    runtime = detect_runtime()
    if not runtime.available or not runtime.executable:
        raise RuntimeError("PRoot-Distro OCI backend is unavailable: " + runtime.message)
    return ProotOCIBackend(runtime.executable)
