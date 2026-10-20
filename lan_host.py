#!/usr/bin/env python3
"""Supervise the existing Termux file server, LAN proxy, and mDNS responder."""
import argparse
import base64
import fcntl
import json
import http.client
import os
from pathlib import Path
import signal
import select
import shutil
import random
import stat
import struct
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from reliability_state import ReliabilityState
from lan_network import current_wifi_ipv4

ROOT = Path(__file__).resolve().parent
def _int_setting(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


POLL_SECONDS = 3
NETWORK_GRACE_SECONDS = 12
SERVER_START_TIMEOUT = 120
PROXY_RETRY_SECONDS = 8
MDNS_RETRY_SECONDS = 30
TUNNEL_RETRY_SECONDS = 20
NETWORK_DIAGNOSTIC_INTERVAL_SECONDS = _int_setting("MYFOREVER_NETWORK_DIAGNOSTIC_SECONDS", 300, 120, 1800)
TUNNEL_DISCONNECTED_GRACE_SECONDS = _int_setting("MYFOREVER_TUNNEL_GRACE_SECONDS", 300, 120, 1800)
INTERNET_PROBE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
def _safe_public_probe_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment):
            return value
    except (TypeError, ValueError):
        pass
    return None


PUBLIC_PROBE_URL = _safe_public_probe_url(os.environ.get("MYFOREVER_PUBLIC_URL", "https://files.costop.in/"))
HEALTH_CHECK_INTERVAL_SECONDS = _int_setting("MYFOREVER_HEALTH_INTERVAL_SECONDS", 30, 10, 300)
HEALTH_FAILURE_THRESHOLD = _int_setting("MYFOREVER_HEALTH_FAILURE_THRESHOLD", 3, 2, 10)
RESOURCE_CHECK_INTERVAL_SECONDS = 60
DEFAULT_LOW_MEMORY_WARNING_BYTES = 256 * 1024 * 1024
LOW_MEMORY_WARNING_BYTES = _int_setting(
    "MYFOREVER_LOW_MEMORY_WARNING_MB",
    DEFAULT_LOW_MEMORY_WARNING_BYTES // (1024 * 1024), 128, 2048
) * 1024 * 1024
TOKEN_FILE = Path.home() / ".config" / "cloudflared" / "token"
TUNNEL_LOG = ROOT / "cloudflared.log"
RESTART_DELAYS = (2, 4, 8, 16, 32, 60)


def restart_delay(failure_count):
    """Bound crash-recovery delays while preventing rapid restart loops."""
    return RESTART_DELAYS[min(max(0, int(failure_count)), len(RESTART_DELAYS) - 1)]


def jittered_restart_delay(failure_count, base=None, maximum=300, jitter_ratio=0.2, rng=None):
    """Bounded retry with jitter; backend keeps its established 2/4/8/16/32/60 schedule."""
    failure_count = max(0, int(failure_count))
    if base is None:
        nominal = min(maximum, restart_delay(failure_count))
    else:
        nominal = min(maximum, max(1, int(base)) * (2 ** min(failure_count, 20)))
    random_source = rng or random
    spread = nominal * max(0.0, min(0.5, float(jitter_ratio)))
    return max(1.0, min(float(maximum), nominal + random_source.uniform(-spread, spread)))


def classify_network_state(local_ready, wifi_available, internet_available, tunnel_connected, public_reachable):
    """Classify observed layers without confusing a healthy local app with public reachability."""
    if local_ready is False:
        return "backend_unhealthy"
    if local_ready in (None, "unverified"):
        return "backend_unverified"
    if local_ready == "database_unavailable":
        return "database_unavailable"
    if wifi_available is False:
        return "wifi_unavailable"
    if internet_available is False:
        return "internet_unavailable"
    if internet_available is None:
        return "internet_unverified"
    if tunnel_connected is False:
        return "tunnel_disconnected"
    if tunnel_connected is None:
        return "tunnel_unverified"
    if public_reachable is False:
        return "public_hostname_unreachable"
    if public_reachable is None:
        return "public_hostname_unverified"
    return "healthy"


def _http_probe(url, timeout=3):
    """Return only reachability and status; response bodies and headers are discarded."""
    request = Request(url, headers={"User-Agent": "MyForever-reliability-check/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            response.read(128)
    except HTTPError as exc:
        status = int(exc.code)
        try:
            exc.close()
        except Exception:
            pass
    except (OSError, URLError, TimeoutError, ValueError):
        return {"reachable": False, "status": None}
    return {"reachable": 200 <= status < 500, "status": status}


def note_slow_retry_mode(journal, service, attempt, threshold=6):
    """Persist one event when a service reaches its slow, capped retry phase."""
    if attempt != threshold:
        return
    records = journal.snapshot().get("recoveryAttemptsExceeded") or []
    records = (records + [{"service": service, "at": time.time(), "attempt": attempt}])[-10:]
    journal.update(recoveryAttemptsExceeded=records)
    journal.record_event("ERROR", service, "slow_retry_mode", "Repeated recovery failures reached the backoff ceiling; retries continue at the capped interval.")


def process_start_ticks(pid):
    """Return Linux proc start time for PID-reuse-safe persisted process identities."""
    try:
        stat_line = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = stat_line[stat_line.rfind(")") + 2:].split()
        return int(fields[19])  # field 22 (starttime); the suffix begins at field 3.
    except (OSError, ValueError, IndexError, TypeError):
        return None


def process_argv(pid):
    """Read argv for a same-UID process; inaccessible or exited processes are unknown."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        argv = tuple(item.decode("utf-8", "replace") for item in raw.split(b"\0") if item)
        return argv or None
    except OSError:
        return None


class ManagedProcessReference:
    """Safely monitor/adopt a previously managed orphan by PID and argv identity."""

    def __init__(self, pid, matcher, label, start_ticks=None):
        self.pid = int(pid)
        self.matcher = matcher
        self.label = label
        self._returncode = None
        self._pidfd = None
        if not self.matches() or (start_ticks is not None and process_start_ticks(self.pid) != start_ticks):
            raise ProcessLookupError(f"PID is not the expected {label} process")
        if hasattr(os, "pidfd_open"):
            try:
                self._pidfd = os.pidfd_open(self.pid, 0)
            except OSError:
                self._pidfd = None

    def matches(self):
        argv = process_argv(self.pid)
        return bool(argv and self.matcher(argv))

    @property
    def returncode(self):
        return self._returncode

    def poll(self):
        if self._returncode is not None:
            return self._returncode
        if self._pidfd is not None:
            try:
                if select.select([self._pidfd], [], [], 0)[0]:
                    self._returncode = 0
                    self.close()
                    return self._returncode
            except OSError:
                pass
        if not self.matches():
            self._returncode = 0
            self.close()
        return self._returncode

    def _send_signal(self, signum):
        if self.poll() is not None:
            return
        try:
            if self._pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                signal.pidfd_send_signal(self._pidfd, signum)
            elif self.matches():
                os.kill(self.pid, signum)
        except ProcessLookupError:
            self._returncode = 0

    def terminate(self):
        self._send_signal(signal.SIGTERM)

    def kill(self):
        self._send_signal(signal.SIGKILL)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.label, timeout)
            time.sleep(0.1)
        return self._returncode

    def close(self):
        if self._pidfd is not None:
            try:
                os.close(self._pidfd)
            except OSError:
                pass
            self._pidfd = None


def _script_matcher(script_path):
    target = str(Path(script_path).resolve())
    return lambda argv: target in argv


def _tunnel_matcher(argv):
    return bool(argv and Path(argv[0]).name == "cloudflared"
                and "tunnel" in argv and "run" in argv
                and "--token-file" in argv and str(TOKEN_FILE) in argv)


def adopt_managed_process(identity, matcher, label):
    if isinstance(identity, dict):
        pid, start_ticks = identity.get("pid"), identity.get("startTicks")
    else:
        pid, start_ticks = identity, None
    try:
        return ManagedProcessReference(pid, matcher, label, start_ticks)
    except (OSError, ProcessLookupError, ValueError, TypeError):
        return None


def process_identity(proc):
    if proc is None:
        return None
    pid = getattr(proc, "pid", None)
    ticks = process_start_ticks(pid) if isinstance(pid, int) else None
    return {"pid": pid, "startTicks": ticks} if ticks is not None else None


def process_memory_bytes(pid):
    """Read RSS and peak RSS for a known child PID without spawning a monitor process."""
    if not isinstance(pid, int) or pid <= 0:
        return {"rssBytes": None, "peakRssBytes": None}
    values = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key, sep, rest = line.partition(":")
            if sep and key in ("VmRSS", "VmHWM"):
                values[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {"rssBytes": None, "peakRssBytes": None}
    return {"rssBytes": values.get("VmRSS"), "peakRssBytes": values.get("VmHWM")}


def bound_log_file(path, maximum_bytes=2 * 1024 * 1024, retained_bytes=1024 * 1024):
    """Compact a known append-only log in place, retaining only its newest bounded tail."""
    path = Path(path)
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid():
            return False
        if stat.S_IMODE(info.st_mode) & 0o077:
            path.chmod(0o600)
        if info.st_size <= maximum_bytes:
            return False
        with path.open("r+b") as log:
            log.seek(max(0, info.st_size - retained_bytes))
            tail = log.read(retained_bytes)
            log.seek(0)
            log.write(tail)
            log.truncate()
            log.flush()
            os.fsync(log.fileno())
        return True
    except OSError:
        return False


def memory_pressure_snapshot():
    """Collect available RAM, kernel pressure (if exposed), and OOM counter (if exposed)."""
    result = resource_snapshot()
    result.update({"pressureAvg10": None, "pressureFullAvg10": None, "oomKills": None})
    try:
        for line in Path("/proc/pressure/memory").read_text().splitlines():
            fields = dict(item.split("=", 1) for item in line.split()[1:] if "=" in item)
            value = float(fields["avg10"])
            if line.startswith("some "):
                result["pressureAvg10"] = value
            elif line.startswith("full "):
                result["pressureFullAvg10"] = value
    except (OSError, ValueError, KeyError):
        pass
    try:
        for line in Path("/proc/vmstat").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                result["oomKills"] = int(value)
                break
    except (OSError, ValueError):
        pass
    return result


def memory_pressure_level(snapshot, warning_bytes=LOW_MEMORY_WARNING_BYTES, critical_bytes=160 * 1024 * 1024):
    available = snapshot.get("availableMemoryBytes")
    if available is None:
        return "unavailable"
    pressure = snapshot.get("pressureAvg10")
    if available < critical_bytes or (pressure is not None and pressure >= 20):
        return "critical"
    if available < warning_bytes or (pressure is not None and pressure >= 5):
        return "warning"
    return "normal"


def health_recovery_decision(state, consecutive_failures, threshold=HEALTH_FAILURE_THRESHOLD):
    """Decide whether to keep, wait, or restart without treating DB outages as app crashes."""
    if state == "ready":
        return "keep", 0
    if state in ("database_unavailable", "unverified"):
        return "wait", 0
    failures = max(0, int(consecutive_failures)) + 1
    return ("restart" if failures >= max(1, int(threshold)) else "wait"), failures


def server_readiness(password_file=None, timeout=3):
    """Probe authenticated local readiness without logging credentials or response data."""
    path = Path(password_file or os.environ.get(
        "TERMUX_SERVER_PASSWORD_FILE",
        str(Path.home() / ".config" / "myforever" / "server_password"),
    )).expanduser()
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            return "unverified", "private password file permissions are invalid"
        password = path.read_text(encoding="utf-8").rstrip("\r\n")
        if not password or "\x00" in password:
            return "unverified", "private password file is empty or invalid"
    except (OSError, UnicodeError):
        return "unverified", "private password file is unavailable or invalid"

    credential = base64.b64encode(("admin:" + password).encode("utf-8")).decode("ascii")
    conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=timeout)
    try:
        conn.request("GET", "/api/health/ready", headers={"Authorization": "Basic " + credential})
        response = conn.getresponse()
        status = response.status
        payload = response.read(4096)
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError):
            body = {}
        if status == 401:
            return "unverified", "readiness authentication was rejected"
        if body.get("database") == "unavailable":
            return "database_unavailable", "database readiness check failed"
        if status == 200 and body.get("state") == "ready":
            return "ready", "local API and database are ready"
        if status >= 500:
            return "unhealthy", f"readiness endpoint returned HTTP {status}"
        return "unhealthy", f"readiness endpoint returned unexpected HTTP {status}"
    except (OSError, http.client.HTTPException, TimeoutError):
        return "unhealthy", "local readiness endpoint did not respond"
    finally:
        conn.close()


def resource_snapshot(root=ROOT):
    """Return aggregate resource readings; unavailable values remain None."""
    available = storage_total = storage_used = free_storage = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        usage = shutil.disk_usage(root)
        storage_total, storage_used, free_storage = usage.total, usage.used, usage.free
    except OSError:
        pass
    return {"availableMemoryBytes": available, "totalStorageBytes": storage_total,
            "usedStorageBytes": storage_used, "freeStorageBytes": free_storage}


def current_lan_ipv4():
    """Prefer the active wlan interface; the helper falls back to the route source."""
    return current_wifi_ipv4()


def server_response(address="127.0.0.1", port=8000):
    try:
        conn = http.client.HTTPConnection(address, port, timeout=2)
        conn.request("GET", "/")
        response = conn.getresponse()
        server = response.getheader("Server", "")
        status = response.status
        response.read(1024)
        conn.close()
        if "TermuxFileServer" not in server:
            return None
        return status
    except (OSError, http.client.HTTPException):
        return None


def ensure_server(manage_server, previous_identity=None):
    status = server_response()
    if status is not None:
        if manage_server and previous_identity:
            adopted = adopt_managed_process(previous_identity, _script_matcher(ROOT / "server.py"), "server.py")
            if adopted is not None:
                print(f"Adopted previously managed server.py process (pid {adopted.pid}) after supervisor recovery.", flush=True)
                return adopted
        print(f"Existing file server is responding on 127.0.0.1:8000 (HTTP {status}); leaving it unmanaged.", flush=True)
        return None
    if not manage_server:
        raise RuntimeError("server.py is not responding on 127.0.0.1:8000. Start it, or use start_myserver.sh.")
    # An unrelated listener is a configuration conflict; never kill or replace it.
    try:
        sock = socket.create_connection(("127.0.0.1", 8000), timeout=1)
    except OSError:
        sock = None
    if sock is not None:
        sock.close()
        raise RuntimeError("Port 8000 is occupied by a non-TermuxFileServer service; leaving it untouched.")
    print("Starting server.py; enter its password when prompted.", flush=True)
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "server.py")], cwd=ROOT)
    try:
        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server.py exited during startup (status {proc.returncode}).")
            status = server_response()
            if status is not None:
                print(f"File server is responding on 127.0.0.1:8000 (HTTP {status}).", flush=True)
                return proc
            time.sleep(1)
        raise RuntimeError("Timed out waiting for server.py on port 8000; check the password prompt and server output.")
    except BaseException:
        stop(proc)
        raise


def stop(proc):
    if proc is None:
        return
    if proc.poll() is not None:
        close = getattr(proc, "close", None)
        if close:
            close()
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    finally:
        close = getattr(proc, "close", None)
        if close:
            close()


def proxy_response(address):
    try:
        conn = http.client.HTTPConnection(address, 80, timeout=2)
        conn.request("GET", "/")
        response = conn.getresponse()
        server = response.getheader("Server", "")
        status = response.status
        response.read(1024)
        conn.close()
        if "TermuxLANProxy" in server and status == 401:
            return status
    except (OSError, http.client.HTTPException):
        pass
    return None


def start_proxy(address, previous_identity=None):
    adopted = adopt_managed_process(previous_identity, _script_matcher(ROOT / "lan_access.py"), "LAN proxy") if previous_identity else None
    if adopted is not None:
        print(f"Adopted previously managed LAN proxy (pid {adopted.pid}) after supervisor recovery.", flush=True)
        return adopted
    env = os.environ.copy()
    env["TERMUX_LAN_BIND_ADDRESS"] = address
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "lan_access.py")], cwd=ROOT, env=env)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"Port-80 proxy failed to start (exit {proc.returncode}); check the port-80 bind error above.", flush=True)
            return None
        status = proxy_response(address)
        if status is not None:
            print(f"Port-80 proxy verified on {address}:80 (HTTP {status}, forwarded to port 8000).", flush=True)
            return proc
        time.sleep(0.25)
    print(f"Port-80 process is alive on {address}, but an HTTP forwarding check did not succeed.", flush=True)
    return proc


def mdns_response_valid(address):
    """Send a real ephemeral-port multicast A question and validate the RFC 6762 reply."""
    query_id = int.from_bytes(os.urandom(2), "big")
    qname = b"".join(bytes((len(label),)) + label.encode("ascii") for label in "myserver.local".split(".")) + b"\0"
    question = qname + struct.pack("!HH", 1, 1)
    query = struct.pack("!HHHHHH", query_id, 0, 1, 0, 0, 0) + question
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(0.7)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.sendto(query, ("224.0.0.251", 5353))
        response, _peer = sock.recvfrom(4096)
        ident, flags, qd, an, _ns, _ar = struct.unpack_from("!HHHHHH", response, 0)
        return (ident == query_id and flags & 0x8400 == 0x8400 and qd == 1 and an >= 1
                and response[12:12 + len(question)] == question
                and response.endswith(socket.inet_aton(address)))
    except (OSError, struct.error):
        return False
    finally:
        sock.close()


def start_mdns(previous_identity=None):
    adopted = adopt_managed_process(previous_identity, _script_matcher(ROOT / "mdns_advertise.py"), "mDNS responder") if previous_identity else None
    if adopted is not None:
        print(f"Adopted previously managed mDNS responder (pid {adopted.pid}) after supervisor recovery.", flush=True)
        return adopted
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "mdns_advertise.py")], cwd=ROOT)
    time.sleep(0.35)
    if proc.poll() is not None:
        print(f"mDNS responder failed to start (exit {proc.returncode}).", flush=True)
        return None
    print(f"Started mDNS responder (pid {proc.pid}); it will report multicast join or conflict status.", flush=True)
    return proc


def tunnel_connections():
    """Return active connector count from cloudflared's loopback-only metrics endpoint."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 20241, timeout=2)
        conn.request("GET", "/metrics")
        response = conn.getresponse()
        body = response.read(128_000).decode("utf-8", "replace")
        conn.close()
        if response.status != 200:
            return None
        for line in body.splitlines():
            if line.startswith("cloudflared_tunnel_ha_connections "):
                return int(float(line.rsplit(" ", 1)[1]))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return None


def is_cloudflared_tunnel_run(cmdline):
    """Recognize cloudflared args independent of option placement/order."""
    tokens = str(cmdline).split()
    return bool(tokens and Path(tokens[0]).name == "cloudflared"
                and "tunnel" in tokens and "run" in tokens)


def other_tunnel_process_exists():
    """Avoid starting a second connector that is not owned by this supervisor."""
    try:
        result = subprocess.run(["ps", "-ef"], capture_output=True, text=True, timeout=3)
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) >= 3 and is_cloudflared_tunnel_run(fields[2]):
                try:
                    if int(fields[1]) != os.getpid():
                        return True
                except ValueError:
                    continue
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_tunnel(previous_identity=None):
    adopted = adopt_managed_process(previous_identity, _tunnel_matcher, "cloudflared") if previous_identity else None
    if adopted is not None:
        print(f"Adopted previously managed cloudflared process (pid {adopted.pid}) after supervisor recovery.", flush=True)
        return adopted
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        print("Cloudflare tunnel is configured but cloudflared is not installed.", flush=True)
        return None
    if not TOKEN_FILE.is_file() or not TOKEN_FILE.stat().st_size:
        print(f"Cloudflare tunnel is not configured; private token file is missing: {TOKEN_FILE}", flush=True)
        return None
    if other_tunnel_process_exists():
        print("Another cloudflared tunnel run process exists; not starting a duplicate.", flush=True)
        return None
    try:
        if TUNNEL_LOG.is_file() and TUNNEL_LOG.stat().st_size >= 2 * 1024 * 1024:
            oldest = Path(str(TUNNEL_LOG) + ".2")
            previous = Path(str(TUNNEL_LOG) + ".1")
            if oldest.exists():
                oldest.unlink()
            if previous.exists():
                previous.replace(oldest)
            TUNNEL_LOG.replace(previous)
        TUNNEL_LOG.touch(mode=0o600, exist_ok=True)
        TUNNEL_LOG.chmod(0o600)
    except OSError as exc:
        print(f"Could not prepare private, size-limited tunnel log ({type(exc).__name__}); leaving connector stopped.", flush=True)
        return None
    args = [cloudflared, "tunnel", "--no-autoupdate", "--loglevel", "info",
            "--logfile", str(TUNNEL_LOG), "--metrics", "127.0.0.1:20241",
            "run", "--token-file", str(TOKEN_FILE)]
    try:
        proc = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print(f"Could not start cloudflared: {exc}", flush=True)
        return None
    time.sleep(1)
    if proc.poll() is not None:
        print(f"cloudflared exited during startup (status {proc.returncode}); inspect cloudflared.log.", flush=True)
        return None
    print(f"Started cloudflared connector (pid {proc.pid}); checking its Cloudflare connections.", flush=True)
    return proc


def main():
    parser = argparse.ArgumentParser(description="Supervise the Termux file server LAN services.")
    parser.add_argument("--manage-server", action="store_true",
                        help="start server.py if it is not already responding on port 8000")
    args = parser.parse_args()

    lock_path = Path(tempfile.gettempdir()) / f"termux-lan-host-{os.getuid()}.lock"
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit("Another lan_host.py supervisor is already running; leaving all services untouched.")

    journal = ReliabilityState()
    prior = journal.snapshot()
    previous_pids = prior.get("managedPids") or {}
    if prior.get("lastUnexpectedExit"):
        print("Recovery report: previous unexpected service exit is recorded; checking current local services.", flush=True)
    journal.update(startedAt=time.time(), serviceState={"overall": "RECOVERING", "backend": "starting", "database": "checking", "tunnel": "starting"})
    journal.record_event("INFO", "supervisor", "supervisor_started", "Supervisor process started; local backend recovery is independent of tunnel connectivity.")

    server = proxy = mdns = tunnel = None
    owns_server = False
    stopping = False
    bound_address = None
    route_missing_since = None
    last_route_error = None
    proxy_retry_at = 0.0
    mdns_retry_at = 0.0
    mdns_probe_at = 0.0
    mdns_verified_address = None
    mdns_wait_logged = False
    tunnel_retry_at = 0.0
    tunnel_failure_count = 0
    tunnel_no_edges_since = None
    tunnel_restart_after_disconnect_at = 0.0
    tunnel_reported_connected = False
    tunnel_config_notice = False
    restart_requested = False
    restart_count = 0
    server_retry_at = 0.0
    server_started_at = 0.0
    stable_uptime_recorded = False
    health_failures = 0
    next_health_check = time.monotonic() + HEALTH_CHECK_INTERVAL_SECONDS
    next_resource_check = time.monotonic()
    next_log_maintenance = time.monotonic() + 60
    next_network_diagnostic = time.monotonic()
    last_network_classification = None
    last_network_snapshot = {"classification": "unverified", "wifi": None, "internet": None,
                             "tunnel": None, "publicEndpoint": None, "checkedAt": None}
    last_oom_kills = None
    last_readiness_state = "unverified"
    low_memory_warning = False
    database_warning = False
    readiness_warning = False

    def record_managed_pids():
        journal.update(managedPids={
            "backend": process_identity(server) if owns_server else None,
            "proxy": process_identity(proxy),
            "mdns": process_identity(mdns),
            "tunnel": process_identity(tunnel),
        })

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def request_server_restart(_signum, _frame):
        nonlocal restart_requested
        restart_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, request_server_restart)
    try:
        shared_path = ROOT / "shared"
        if not (shared_path.is_dir() and os.access(shared_path, os.R_OK | os.W_OK)):
            journal.record_event("ERROR", "supervisor", "storage_unavailable", "Persistent shared storage is missing or not readable/writable; no files were changed.")
            journal.update(serviceState={"overall": "DEGRADED", "backend": "starting", "database": "unknown", "tunnel": "waiting_for_backend"})
            print("Persistent shared storage is missing or not writable; preserving data and reporting degraded startup.", flush=True)
        else:
            print("Persistent shared storage is accessible.", flush=True)
        try:
            server = ensure_server(args.manage_server, previous_pids.get("backend"))
            owns_server = server is not None
            record_managed_pids()
            if owns_server:
                server_started_at = time.monotonic()
            journal.update(serviceState={"overall": "DEGRADED", "backend": "healthy", "database": "checking", "tunnel": "starting"})
            journal.record_event("INFO", "backend", "backend_http_ready", "Backend HTTP listener verified on loopback port 8000.")
        except RuntimeError as exc:
            if not args.manage_server:
                raise
            restart_count = 1
            delay = jittered_restart_delay(0)
            server_retry_at = time.monotonic() + delay
            journal.record_event("ERROR", "backend", "startup_failed", "Initial backend startup failed; a bounded retry is scheduled.")
            journal.update(serviceState={"overall": "RECOVERING", "backend": "recovering", "database": "unknown", "tunnel": "waiting_for_backend"})
            print(f"Initial file-server startup failed: {exc}; retrying in {delay:.1f}s.", flush=True)
        print("LAN supervisor active. Press Ctrl+C to stop its children.", flush=True)
        while not stopping:
            now = time.monotonic()
            if restart_requested:
                restart_requested = False
                if owns_server and server is not None:
                    print("Requested file-server restart; stopping only the server child managed by this supervisor.", flush=True)
                    stop(server)
                    server = None
                    owns_server = False
                    record_managed_pids()
                    restart_count = 0
                    stable_uptime_recorded = False
                    server_retry_at = time.monotonic() + 1
                else:
                    print("Requested file-server restart ignored: this supervisor does not own the existing server process.", flush=True)
            if owns_server and server is not None and not stable_uptime_recorded and now - server_started_at >= 60:
                restart_count = 0
                stable_uptime_recorded = True
            if owns_server and server is not None and server.poll() is not None:
                code = server.returncode
                if now - server_started_at >= 60:
                    restart_count = 0
                server = None
                owns_server = False
                record_managed_pids()
                delay = jittered_restart_delay(restart_count)
                restart_count += 1
                server_retry_at = now + delay
                journal.increment_restart("backend")
                journal.update(lastUnexpectedExit={"service": "backend", "at": time.time(), "exitStatus": code},
                               serviceState={"overall": "RECOVERING", "backend": "recovering", "database": "unknown", "tunnel": "unchanged"})
                journal.record_event("ERROR", "backend", "unexpected_exit", f"Managed backend exited with status {code}; retry scheduled.")
                print(f"Managed server.py exited (status {code}); restart attempt scheduled in {delay:.1f}s.", flush=True)
            if args.manage_server and server is None and now >= server_retry_at:
                try:
                    server = ensure_server(True, previous_pids.get("backend"))
                    owns_server = server is not None
                    record_managed_pids()
                    if owns_server:
                        previous_pids["backend"] = process_identity(server)
                        server_started_at = time.monotonic()
                        stable_uptime_recorded = False
                        journal.update(lastSuccessfulRecovery={"service": "backend", "at": time.time()},
                                       serviceState={"overall": "DEGRADED", "backend": "healthy", "database": "checking", "tunnel": "unchanged"})
                        journal.record_event("INFO", "backend", "recovery_succeeded", "Managed backend HTTP listener recovered.")
                        print("Managed file server recovered and is responding on port 8000.", flush=True)
                    else:
                        server_retry_at = now + POLL_SECONDS
                except RuntimeError as exc:
                    delay = jittered_restart_delay(restart_count)
                    restart_count += 1
                    server_retry_at = now + delay
                    journal.increment_restart("backend")
                    journal.record_event("ERROR", "backend", "recovery_failed", "Backend recovery attempt failed; retry uses bounded backoff.")
                    if restart_count >= len(RESTART_DELAYS):
                        attempts = (journal.snapshot().get("recoveryAttemptsExceeded") or [])[-9:]
                        attempts.append({"service": "backend", "at": time.time(), "attempts": restart_count})
                        journal.update(recoveryAttemptsExceeded=attempts)
                        journal.record_event("ERROR", "backend", "retries_limited", "Backend retries reached the configured backoff ceiling; automatic retries remain rate-limited.")
                    print(f"File-server recovery attempt failed: {exc}; retrying in {delay:.1f}s.", flush=True)
            if owns_server and server is not None and now >= next_health_check:
                next_health_check = now + HEALTH_CHECK_INTERVAL_SECONDS
                readiness, detail = server_readiness()
                last_readiness_state = readiness
                action, health_failures = health_recovery_decision(
                    readiness, health_failures
                )
                if readiness == "ready":
                    if health_failures == 0 and database_warning:
                        print("Local API and database readiness recovered.", flush=True)
                    database_warning = False
                    readiness_warning = False
                    saved_network = journal.snapshot().get("networkState") or {}
                    core_overall = "HEALTHY" if saved_network.get("classification") == "healthy" else "DEGRADED"
                    journal.update(lastSuccessfulHealthCheck=time.time(), serviceState={"overall": core_overall, "backend": "healthy", "database": "healthy", "tunnel": "unchanged"})
                elif readiness == "database_unavailable":
                    journal.update(serviceState={"overall": "DEGRADED", "backend": "healthy", "database": "degraded", "tunnel": "unchanged"})
                    journal.record_event("WARN", "database", "database_unavailable", "SQLite readiness check failed; backend was preserved and no database reset was attempted.")
                    if not database_warning:
                        print("Database readiness is unavailable; preserving the backend and diagnosing via the read-only health endpoint.", flush=True)
                        database_warning = True
                elif readiness == "unverified":
                    journal.update(serviceState={"overall": "DEGRADED", "backend": "unverified", "database": "unverified", "tunnel": "unchanged"})
                    if not readiness_warning:
                        print(f"Authenticated readiness could not be verified ({detail}); preserving the running backend.", flush=True)
                        readiness_warning = True
                else:
                    journal.update(serviceState={"overall": "RECOVERING", "backend": "unhealthy", "database": "unknown", "tunnel": "unchanged"})
                    journal.record_event("WARN", "backend", "readiness_failed", f"Local readiness failed ({health_failures}/{HEALTH_FAILURE_THRESHOLD}); backend recovery threshold is enforced.")
                    print(f"Backend readiness check failed ({health_failures}/{HEALTH_FAILURE_THRESHOLD}): {detail}.", flush=True)
                    if action == "restart":
                        delay = jittered_restart_delay(restart_count)
                        print(f"Backend process is alive but repeatedly unhealthy; restarting only its managed server child after {delay}s.", flush=True)
                        stop(server)
                        server = None
                        owns_server = False
                        record_managed_pids()
                        health_failures = 0
                        restart_count += 1
                        journal.increment_restart("backend")
                        journal.update(lastUnexpectedExit={"service": "backend", "at": time.time(), "exitStatus": "health_check_restart"},
                                       serviceState={"overall": "RECOVERING", "backend": "recovering", "database": "unknown", "tunnel": "unchanged"})
                        journal.record_event("WARN", "backend", "health_recovery", "Repeated local readiness failures triggered a backend-only restart.")
                        server_retry_at = time.monotonic() + delay
                        next_health_check = server_retry_at + HEALTH_CHECK_INTERVAL_SECONDS
                        stable_uptime_recorded = False
            if now >= next_resource_check:
                next_resource_check = now + RESOURCE_CHECK_INTERVAL_SECONDS
                resources = memory_pressure_snapshot()
                available = resources["availableMemoryBytes"]
                pressure = memory_pressure_level(resources)
                is_low = pressure in ("warning", "critical")
                if is_low and not low_memory_warning:
                    mib = available // (1024 * 1024) if available is not None else "unavailable"
                    journal.record_event("WARN", "memory", "memory_pressure", f"System memory pressure is {pressure}; optional model loads are deferred by their existing memory guard.")
                    print(f"Memory pressure {pressure}: {mib} MiB available; core hosting remains prioritized and no unrelated process is terminated.", flush=True)
                elif low_memory_warning and not is_low:
                    journal.record_event("INFO", "memory", "memory_recovered", "Available memory returned above the warning threshold.")
                    print("Available memory recovered above the configured warning threshold.", flush=True)
                low_memory_warning = is_low
                oom_kills = resources.get("oomKills")
                if last_oom_kills is not None and oom_kills is not None and oom_kills > last_oom_kills:
                    journal.record_event("WARN", "memory", "oom_counter_increased", "Kernel OOM-kill counter increased since the prior sample; individual Android process attribution is unavailable.")
                last_oom_kills = oom_kills
                backend_pid = server.pid if owns_server and server is not None else None
                tunnel_pid = tunnel.pid if tunnel is not None and tunnel.poll() is None else None
                processes = {"backend": process_memory_bytes(backend_pid), "tunnel": process_memory_bytes(tunnel_pid),
                             "database": {"kind": "embedded SQLite", "separateProcess": False, "rssBytes": None},
                             "optionalAI": {"kind": "inference child; measured by authenticated health API", "rssBytes": None}}
                checked_at = time.time()
                memory_data = {"availableBytes": available, "totalStorageBytes": resources.get("totalStorageBytes"),
                               "freeStorageBytes": resources.get("freeStorageBytes"), "pressureAvg10": resources.get("pressureAvg10"),
                               "pressureFullAvg10": resources.get("pressureFullAvg10"), "oomKills": oom_kills,
                               "checkedAt": checked_at, "pressure": pressure, "processes": processes}
                journal.update(memoryState=memory_data, processes=processes)
                if resources["freeStorageBytes"] is None:
                    print("Storage availability could not be measured; no cleanup was attempted.", flush=True)
            try:
                routed = current_lan_ipv4()
                route_missing_since = None
                last_route_error = None
            except OSError as exc:
                routed = None
                if route_missing_since is None:
                    route_missing_since = now
                    last_route_error = str(exc)
                    print(f"Wi-Fi route temporarily unavailable; keeping current listeners for up to {NETWORK_GRACE_SECONDS}s: {exc}", flush=True)
                if now - route_missing_since < NETWORK_GRACE_SECONDS:
                    routed = bound_address
                elif bound_address is not None:
                    print(f"No private Wi-Fi route for {NETWORK_GRACE_SECONDS}s; stopping the old-address proxy. {last_route_error}", flush=True)

            if routed != bound_address:
                mdns_verified_address = None
                mdns_wait_logged = False
                stop(proxy)
                proxy = None
                record_managed_pids()
                bound_address = routed
                proxy_retry_at = now
                if routed:
                    proxy = start_proxy(routed, previous_pids.get("proxy"))
                    record_managed_pids()
                    if proxy is None:
                        proxy_retry_at = now + PROXY_RETRY_SECONDS
                else:
                    print("Proxy is waiting for a private Wi-Fi IPv4 address.", flush=True)

            if mdns is None and now >= mdns_retry_at:
                mdns = start_mdns(previous_pids.get("mdns"))
                record_managed_pids()
                if mdns is None:
                    mdns_retry_at = now + MDNS_RETRY_SECONDS
            elif mdns is not None and mdns.poll() is not None:
                code = mdns.returncode
                mdns = None
                record_managed_pids()
                mdns_retry_at = now + MDNS_RETRY_SECONDS
                mdns_verified_address = None
                print(f"mDNS responder exited (status {code}); retrying in {MDNS_RETRY_SECONDS}s.", flush=True)

            if mdns is not None and bound_address and mdns_verified_address != bound_address and now >= mdns_probe_at:
                if mdns_response_valid(bound_address):
                    mdns_verified_address = bound_address
                    mdns_wait_logged = False
                    print(f"mDNS verified: myserver.local A {bound_address} answered on UDP 5353.", flush=True)
                else:
                    mdns_probe_at = now + 3
                    if not mdns_wait_logged:
                        print(f"Waiting for a valid mDNS A response for {bound_address}.", flush=True)
                        mdns_wait_logged = True

            if TOKEN_FILE.is_file() and TOKEN_FILE.stat().st_size:
                if tunnel is None and now >= tunnel_retry_at and server_response() is None:
                    tunnel_retry_at = now + POLL_SECONDS
                    journal.update_service_component("tunnel", "waiting_for_backend")
                elif tunnel is None and now >= tunnel_retry_at:
                    tunnel = start_tunnel(previous_pids.get("tunnel"))
                    record_managed_pids()
                    if tunnel is not None:
                        previous_pids["tunnel"] = process_identity(tunnel)
                    if tunnel is None:
                        delay = jittered_restart_delay(tunnel_failure_count, base=TUNNEL_RETRY_SECONDS)
                        tunnel_failure_count += 1
                        tunnel_retry_at = now + delay
                        note_slow_retry_mode(journal, "tunnel", tunnel_failure_count)
                        journal.update_service_component("tunnel", "recovering")
                        journal.record_event("WARN", "tunnel", "start_failed", "Cloudflare connector did not start; backend and LAN service remain untouched.")
                        print(f"Cloudflare tunnel start failed; retrying in {delay:.1f}s without restarting the backend.", flush=True)
                elif tunnel is not None and tunnel.poll() is not None:
                    code = tunnel.returncode
                    tunnel = None
                    record_managed_pids()
                    delay = jittered_restart_delay(tunnel_failure_count, base=TUNNEL_RETRY_SECONDS)
                    tunnel_failure_count += 1
                    tunnel_retry_at = now + delay
                    note_slow_retry_mode(journal, "tunnel", tunnel_failure_count)
                    tunnel_reported_connected = False
                    tunnel_no_edges_since = None
                    journal.increment_restart("tunnel")
                    journal.update_service_component("tunnel", "recovering")
                    journal.record_event("WARN", "tunnel", "unexpected_exit", f"Managed cloudflared exited with status {code}; only the tunnel will be retried.")
                    print(f"cloudflared exited (status {code}); retrying in {delay:.1f}s.", flush=True)
                elif tunnel is not None:
                    connections = tunnel_connections()
                    if connections and not tunnel_reported_connected:
                        tunnel_failure_count = 0
                        tunnel_no_edges_since = None
                        journal.update(lastSuccessfulRecovery={"service": "tunnel", "at": time.time()})
                        journal.update_service_component("tunnel", "healthy")
                        journal.record_event("INFO", "tunnel", "connected", f"Cloudflare reports {connections} active edge connections.")
                        print(f"Cloudflare tunnel connected ({connections} active edge connections).", flush=True)
                        tunnel_reported_connected = True
                    elif connections == 0 and tunnel_reported_connected:
                        print("Cloudflare tunnel currently has no active edge connections; allowing cloudflared to recover normally.", flush=True)
                        journal.record_event("WARN", "tunnel", "edges_disconnected", "Tunnel process remains running with no active metrics edges; observing before a tunnel-only restart.")
                        tunnel_reported_connected = False
                        tunnel_no_edges_since = now
                    elif connections == 0 and tunnel_no_edges_since is None:
                        tunnel_no_edges_since = now
                    elif connections and connections > 0:
                        tunnel_no_edges_since = None
                    if (connections == 0 and tunnel_no_edges_since is not None
                            and now - tunnel_no_edges_since >= TUNNEL_DISCONNECTED_GRACE_SECONDS
                            and now >= tunnel_restart_after_disconnect_at
                            and last_network_snapshot.get("internet") is True):
                        delay = jittered_restart_delay(tunnel_failure_count, base=TUNNEL_RETRY_SECONDS)
                        print(f"Tunnel has had no active edges for {TUNNEL_DISCONNECTED_GRACE_SECONDS}s despite internet reachability; restarting only cloudflared after {delay:.1f}s.", flush=True)
                        journal.record_event("WARN", "tunnel", "recovery_restart", "Internet probe succeeds but Cloudflare has no edges; restarting only the managed tunnel connector.")
                        stop(tunnel)
                        tunnel = None
                        record_managed_pids()
                        tunnel_failure_count += 1
                        tunnel_retry_at = now + delay
                        note_slow_retry_mode(journal, "tunnel", tunnel_failure_count)
                        tunnel_restart_after_disconnect_at = tunnel_retry_at + TUNNEL_DISCONNECTED_GRACE_SECONDS
                        journal.increment_restart("tunnel")
            elif not tunnel_config_notice:
                print(f"Cloudflare connector is not configured; add a private token file at {TOKEN_FILE} to enable it.", flush=True)
                tunnel_config_notice = True

            if bound_address and proxy is None and now >= proxy_retry_at:
                proxy = start_proxy(bound_address, previous_pids.get("proxy"))
                record_managed_pids()
                if proxy is None:
                    proxy_retry_at = now + PROXY_RETRY_SECONDS
            elif proxy is not None and proxy.poll() is not None:
                code = proxy.returncode
                proxy = None
                record_managed_pids()
                proxy_retry_at = now + PROXY_RETRY_SECONDS
                print(f"Port-80 proxy exited (status {code}); retrying in {PROXY_RETRY_SECONDS}s.", flush=True)

            if now >= next_log_maintenance:
                next_log_maintenance = now + 60
                supervisor_log = Path.home() / ".local" / "state" / "myforever" / "supervisor.log"
                trimmed = []
                for log_path in (supervisor_log, TUNNEL_LOG):
                    if bound_log_file(log_path):
                        trimmed.append(log_path.name)
                if trimmed:
                    journal.record_event("INFO", "supervisor", "log_compacted", "Bounded recent log tails: " + ", ".join(trimmed) + ".")

            if now >= next_network_diagnostic:
                next_network_diagnostic = now + NETWORK_DIAGNOSTIC_INTERVAL_SECONDS
                internet_result = _http_probe(INTERNET_PROBE_URL)
                internet_ok = internet_result["reachable"]
                public_result = (_http_probe(PUBLIC_PROBE_URL) if internet_ok and PUBLIC_PROBE_URL
                                 else {"reachable": None, "status": None})
                edges = tunnel_connections()
                tunnel_configured = TOKEN_FILE.is_file() and TOKEN_FILE.stat().st_size > 0
                tunnel_ok = ((edges > 0) if edges is not None else (False if tunnel is not None else None))
                if not tunnel_configured and edges is None:
                    tunnel_ok = None
                local_http = server_response() is not None
                ready_state, _ready_detail = server_readiness() if local_http else ("unhealthy", "backend HTTP check failed")
                backend_ready = ("database_unavailable" if ready_state == "database_unavailable" else ready_state == "ready")
                classification = classify_network_state(backend_ready, routed is not None, internet_ok,
                                                        tunnel_ok, public_result["reachable"])
                last_network_snapshot = {"classification": classification, "wifi": routed is not None,
                                         "localHttp": local_http, "backendReady": backend_ready,
                                         "database": "healthy" if ready_state == "ready" else ("degraded" if ready_state == "database_unavailable" else "unknown"),
                                         "internet": internet_ok, "tunnel": tunnel_ok,
                                         "tunnelEdges": edges, "tunnelConfigured": tunnel_configured,
                                         "publicEndpoint": public_result["reachable"],
                                         "publicStatus": public_result["status"], "checkedAt": time.time(),
                                         "source": "device-side probes; not an independent external monitor"}
                journal.update(networkState=last_network_snapshot)
                if classification != last_network_classification:
                    journal.record_event("INFO" if classification == "healthy" else "WARN", "network",
                                         "network_state_changed", f"Connectivity classification changed to {classification}.")
                    last_network_classification = classification
                state_overall = "HEALTHY" if classification == "healthy" else ("OFFLINE" if not local_http else "DEGRADED")
                if last_readiness_state not in ("ready", "unverified") and local_http:
                    state_overall = "DEGRADED"
                tunnel_state = ("not_configured" if not tunnel_configured else
                                "healthy" if tunnel_ok else "recovering" if tunnel is None else "degraded")
                journal.update(serviceState={"overall": state_overall,
                                             "backend": "healthy" if local_http else "unhealthy",
                                             "database": last_network_snapshot["database"],
                                             "tunnel": tunnel_state})
                print(f"Network health: local={'healthy' if local_http else 'unhealthy'}, Wi-Fi={'connected' if routed else 'unavailable'}, internet={'available' if internet_ok else 'unavailable'}, tunnel={edges if edges is not None else 'unverified'} edges, public={'reachable' if public_result['reachable'] else 'unreachable/unverified'}.", flush=True)

            time.sleep(POLL_SECONDS)
    except RuntimeError as exc:
        print(f"Startup error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    finally:
        stop(proxy)
        stop(mdns)
        stop(tunnel)
        if owns_server:
            stop(server)
            journal.update(serviceState={"overall": "OFFLINE", "backend": "stopped", "database": "stopped", "tunnel": "stopping"})
            journal.record_event("INFO", "supervisor", "intentional_shutdown", "Supervisor received an intentional shutdown signal.")
            print("Stopped the file server started by this supervisor.", flush=True)
        else:
            print("Left the pre-existing file server running.", flush=True)
        journal.update(managedPids={"backend": None, "proxy": None, "mdns": None, "tunnel": None})
        lock.close()
        if stopping:
            print("LAN supervisor stopped cleanly.", flush=True)


if __name__ == "__main__":
    main()
