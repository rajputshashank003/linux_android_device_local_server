"""Safe local diagnostics for the Phase 1 execution-engine foundation."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from .config import DEFAULT_CONFIG
from .process import ManagedProbe, run_output_probe
from .runtime import detect_runtime, namespace_probe


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"MYFOREVER_LOOPBACK_PROBE_OK"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _check(name: str, status: str, message: str, **details: object) -> dict[str, object]:
    return {"name": name, "status": status, "message": message, **details}


def _filesystem_test() -> dict[str, object]:
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix=DEFAULT_CONFIG.temporary_prefix) as folder:
            path = Path(folder) / "probe.txt"
            path.write_text("myforever-execution-probe", encoding="utf-8")
            if path.read_text(encoding="utf-8") != "myforever-execution-probe":
                return _check("Temporary filesystem", "FAIL", "Read-back did not match.")
            size = path.stat().st_size
            path.unlink()
            if path.exists():
                return _check("Temporary filesystem", "FAIL", "Temporary probe file was not removed.")
        return _check("Temporary filesystem", "PASS", "Temporary write/read/remove succeeded; no existing user files were touched.", bytes_written=size, elapsed_seconds=round(time.monotonic() - started, 4))
    except OSError as exc:
        return _check("Temporary filesystem", "FAIL", f"Temporary filesystem probe failed: {exc}")


def _network_test() -> dict[str, object]:
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/probe"
        with urlopen(url, timeout=DEFAULT_CONFIG.http_timeout_seconds) as response:
            body = response.read(128)
            passed = response.status == 200 and body == b"MYFOREVER_LOOPBACK_PROBE_OK"
        return _check("Loopback HTTP", "PASS" if passed else "FAIL", "Ephemeral 127.0.0.1 listener responded; this does not test LAN or public reachability.", interface="127.0.0.1", port=server.server_port)
    except (OSError, TimeoutError) as exc:
        return _check("Loopback HTTP", "FAIL", f"Loopback probe failed: {exc}")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=DEFAULT_CONFIG.http_timeout_seconds)


def _process_memory(pid: int) -> dict[str, object]:
    """Read RSS and virtual size for our own short-lived child, when procfs permits."""
    try:
        values: dict[str, int] = {}
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            key, _, raw = line.partition(":")
            fields = raw.split()
            if key in {"VmRSS", "VmSize"} and fields and fields[0].isdigit():
                values[key] = int(fields[0]) * 1024
        return {"rssBytes": values.get("VmRSS"), "virtualBytes": values.get("VmSize"), "source": f"/proc/{pid}/status"}
    except (OSError, ValueError):
        return {"rssBytes": None, "virtualBytes": None, "source": None}


def _port_conflict_test() -> dict[str, object]:
    first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        first.bind(("127.0.0.1", 0))
        address = first.getsockname()
        first.listen(1)
        try:
            second.bind(address)
        except OSError:
            return _check("Port conflict detection", "PASS", "A loopback port already in use was rejected by a second bind.", interface=address[0])
        return _check("Port conflict detection", "FAIL", "The operating system allowed an unexpected duplicate bind.")
    except OSError as exc:
        return _check("Port conflict detection", "FAIL", f"Could not perform loopback port probe: {exc}")
    finally:
        second.close()
        first.close()


def _process_lifecycle_test() -> dict[str, object]:
    probe = ManagedProbe()
    try:
        start = time.monotonic()
        pid = probe.start()
        startup_seconds = time.monotonic() - start
        if probe.status() != "running":
            return _check("Process lifecycle", "FAIL", "Managed probe did not remain running.")
        process = probe._process
        assert process is not None
        memory = _process_memory(pid)
        process.terminate()
        process.wait(timeout=DEFAULT_CONFIG.probe_timeout_seconds)
        if probe.status() != "exited":
            return _check("Process lifecycle", "FAIL", "Unexpected process termination was not detected.")
        probe.start()
        if probe.status() != "running":
            return _check("Process lifecycle", "FAIL", "Managed probe could not be restarted after exit.")
        probe.stop()
        if probe.status() != "exited":
            return _check("Process lifecycle", "FAIL", "Managed probe did not stop cleanly.")
        return _check("Process lifecycle", "PASS", "Start, inspect, unexpected exit detection, restart, and stop succeeded.", pid=pid, startup_seconds=round(startup_seconds, 4), childMemory=memory, cpuUsagePercent=None, cpuUsageNote="Not measured: the short sleeping lifecycle probe is not a representative CPU workload.")
    except (OSError, RuntimeError, TimeoutError) as exc:
        return _check("Process lifecycle", "FAIL", f"Process lifecycle probe failed: {exc}")
    finally:
        probe.stop()


def _system_snapshot() -> dict[str, object]:
    result: dict[str, object] = {
        "os": platform.platform(), "machine": platform.machine(),
        "python": platform.python_version(), "termux": bool(os.environ.get("TERMUX_VERSION") or os.environ.get("PREFIX", "").startswith("/data/data/com.termux/")),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "is_root": bool(hasattr(os, "geteuid") and os.geteuid() == 0),
        "memory": {"totalBytes": None, "availableBytes": None, "source": None},
        "homeStorage": {"totalBytes": None, "freeBytes": None, "source": None},
    }
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, raw = line.partition(":")
            fields = raw.split()
            if fields and fields[0].isdigit() and key in {"MemTotal", "MemAvailable"}:
                values[key] = int(fields[0]) * 1024
        if "MemTotal" in values:
            result["memory"] = {"totalBytes": values.get("MemTotal"), "availableBytes": values.get("MemAvailable"), "source": "/proc/meminfo (kB × 1024)"}
    except (OSError, ValueError):
        pass
    try:
        usage = shutil.disk_usage(Path.home())
        result["homeStorage"] = {"totalBytes": usage.total, "freeBytes": usage.free, "source": "filesystem containing Termux HOME; not whole-device storage"}
    except OSError:
        pass
    return result


def run_diagnostics() -> dict[str, object]:
    runtime = detect_runtime()
    process = run_output_probe()
    try:
        from .oci_smoke import run_smoke
        smoke = run_smoke()
        smoke_status = smoke.get("status", "NOT VERIFIED")
        if smoke_status in {"SKIPPED", "BLOCKED"}: smoke_status = "NOT VERIFIED"
        smoke_message = smoke.get("message", "OCI smoke test did not return a result.")
    except Exception as exc:
        smoke_status, smoke_message = "FAIL", f"OCI smoke test failed unexpectedly: {type(exc).__name__}"
    checks: list[dict[str, object]] = [
        _check("Runtime detection", "PASS" if runtime.available else "FAIL", runtime.message, runtime=runtime.as_dict()),
        _check("Controlled process execution", process.status, "Fixed Python probe output captured." if process.status == "PASS" else "Fixed Python probe failed.", returncode=process.returncode, stdout=process.stdout, stderr=process.stderr, elapsed_seconds=round(process.elapsed_seconds, 4), timed_out=process.timed_out),
        _filesystem_test(), _process_lifecycle_test(), _network_test(), _port_conflict_test(),
        _check("OCI image execution", smoke_status, smoke_message, image=smoke.get("imageReference"), details={k:smoke[k] for k in ("httpStatus","responseMatches","stopSignal","portReleased") if k in smoke}),
        _check("User namespace creation", **namespace_probe()),
    ]
    base_checks = [c for c in checks if c["name"] not in {"Runtime detection", "OCI image execution", "User namespace creation"}]
    base_ok = all(c["status"] == "PASS" for c in base_checks)
    runtime_ok = runtime.available
    if not base_ok: status="DIAGNOSTICS FAILED"
    elif not runtime_ok: status="BASIC RUNTIME TESTS PASSED; CANDIDATE RUNTIME NOT READY"
    elif smoke_status == "PASS": status="BASIC RUNTIME AND ONE OCI IMAGE SMOKE TEST PASSED"
    else: status="BASIC RUNTIME TESTS PASSED; GENERAL OCI SUPPORT NOT VERIFIED"
    return {
        "title": "MyForever Execution Engine Diagnostics",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "system": _system_snapshot(), "runtime": runtime.as_dict(),
        "checks": checks, "overall": status, "runtimeReady": runtime_ok,
        "basicChecksPassed": base_ok,
        "networkScope": "Loopback only. No LAN, internet, DNS, or Cloudflare test was performed.",
        "securityNote": "PRoot is userspace path emulation, not kernel container isolation. Do not run untrusted images as a security boundary.",
    }


def main() -> int:
    report = run_diagnostics()
    print(report["title"])
    print(f"OS: {report['system']['os']}")
    print(f"Architecture: {report['system']['machine']}")
    print(f"Runtime: {report['runtime']['name']} ({report['runtime']['message']})")
    memory = report["system"]["memory"]
    if memory["totalBytes"] is not None:
        print(f"System RAM: {memory['availableBytes']} bytes available / {memory['totalBytes']} bytes total ({memory['source']})")
    storage = report["system"]["homeStorage"]
    if storage["freeBytes"] is not None:
        print(f"Termux HOME filesystem: {storage['freeBytes']} bytes free / {storage['totalBytes']} bytes total")
    print()
    for item in report["checks"]:
        detail = ""
        if item["name"] == "Process lifecycle" and item["status"] == "PASS":
            sample = item["childMemory"]
            detail = f" startup={item['startup_seconds']}s, child RSS={sample['rssBytes']} bytes" if sample["rssBytes"] is not None else f" startup={item['startup_seconds']}s, child RSS=Unavailable"
        print(f"{item['name']}: {item['status']} — {item['message']}{detail}")
    print(f"\nOverall Status: {report['overall']}")
    print("Network scope: loopback only; public accessibility was not tested.")
    oci_status=next((item["status"] for item in report["checks"] if item["name"]=="OCI image execution"),"NOT VERIFIED")
    return 0 if report["basicChecksPassed"] and report["runtimeReady"] and oci_status in {"PASS","NOT VERIFIED"} else (2 if report["basicChecksPassed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
