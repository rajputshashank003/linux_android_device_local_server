"""Real, loopback-only Nginx OCI smoke test for an explicitly installed test image."""

from __future__ import annotations

import http.client
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG
from .runtime import detect_runtime

IMAGE_NAME = "myfile-phase1-nginx"
EXPECTED_BODY = b"MYFILE_OCI_HTTP_RESPONSE_OK\n"


def _rss_bytes(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            key, _, value = line.partition(":")
            fields = value.split()
            if key == "VmRSS" and fields and fields[0].isdigit():
                return int(fields[0]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(port: int) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
    try:
        conn.request("GET", "/")
        response = conn.getresponse()
        return response.status, response.read(1024)
    finally:
        conn.close()


def run_smoke() -> dict[str, Any]:
    """Run the installed trusted test image; never pulls or installs images."""
    runtime = detect_runtime()
    if not runtime.available:
        return {"status": "BLOCKED", "message": runtime.message, "image": IMAGE_NAME}
    listed = subprocess.run(
        [runtime.executable or "proot-distro", "list", "--quiet"],
        capture_output=True, text=True, timeout=DEFAULT_CONFIG.probe_timeout_seconds,
    )
    if listed.returncode != 0:
        return {"status": "FAIL", "message": "Could not inspect installed PRoot-Distro containers.", "detail": listed.stderr[:1000], "image": IMAGE_NAME}
    if IMAGE_NAME not in listed.stdout.splitlines():
        return {"status": "SKIPPED", "message": f"Test image {IMAGE_NAME!r} is not installed. No image was downloaded.", "image": IMAGE_NAME}

    result: dict[str, Any] = {"status": "FAIL", "image": IMAGE_NAME, "imageReference": "nginx:alpine", "networkScope": "127.0.0.1 only"}
    process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(prefix="myforever-oci-http-") as folder:
        root = Path(folder)
        config = root / "default.conf"
        port = _port()
        config.write_text(
            f'server {{ listen 127.0.0.1:{port}; location / {{ '
            f'default_type text/plain; return 200 "{EXPECTED_BODY.decode().strip()}\n"; }} }}\n',
            encoding="utf-8",
        )
        log_path = root / "runtime.log"
        started = time.monotonic()
        with log_path.open("wb") as log:
            argv = [
                runtime.executable or "proot-distro", "run", "--isolated",
                "--bind", f"{config}:/etc/nginx/conf.d/default.conf", IMAGE_NAME,
            ]
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                close_fds=True,
            )
            result["pid"] = process.pid
            deadline = time.monotonic() + 15
            response: tuple[int, bytes] | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    response = _request(port)
                    break
                except (OSError, TimeoutError, http.client.HTTPException):
                    time.sleep(0.15)
            result["startupSeconds"] = round(time.monotonic() - started, 4)
            result["processRssBytes"] = _rss_bytes(process.pid)
            if response is None:
                result["message"] = "OCI application did not become ready on loopback."
                result["exitCode"] = process.poll()
            else:
                result["httpStatus"] = response[0]
                result["responseMatches"] = response[1] == EXPECTED_BODY
                if response[0] == 200 and response[1] == EXPECTED_BODY:
                    result["status"] = "PASS"
                    result["message"] = "Nginx OCI entrypoint served the expected response on loopback."
                else:
                    result["message"] = "OCI application responded, but the status/body did not match."

            if process.poll() is None:
                # PRoot ignores SIGTERM. SIGQUIT makes PRoot tear down its traced guest processes.
                process.send_signal(signal.SIGQUIT)
                try:
                    result["stopExitCode"] = process.wait(timeout=8)
                    result["stopSignal"] = "SIGQUIT"
                except subprocess.TimeoutExpired:
                    process.kill()
                    result["stopExitCode"] = process.wait(timeout=3)
                    result["stopSignal"] = "SIGKILL fallback"
                    result["status"] = "FAIL"
                    result["message"] = "PRoot did not stop after SIGQUIT; SIGKILL fallback was required."
            else:
                result["stopExitCode"] = process.returncode
                result["stopSignal"] = "process exited before stop"
            log.flush()
        result["runtimeLog"] = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]

    try:
        _request(port)
        result["portReleased"] = False
        result["status"] = "FAIL"
        result["message"] = "Loopback port still responds after stopping the PRoot process."
    except (OSError, TimeoutError, http.client.HTTPException):
        result["portReleased"] = True
    return result


def main() -> int:
    result = run_smoke()
    print(f"OCI smoke test: {result['status']} — {result['message']}")
    if "imageReference" in result:
        print(f"Image: {result['imageReference']} (installed as {result['image']})")
    for key in ("startupSeconds", "processRssBytes", "httpStatus", "responseMatches", "stopSignal", "stopExitCode", "portReleased"):
        if key in result:
            print(f"{key}: {result[key]}")
    if result.get("runtimeLog"):
        lines = [line for line in result["runtimeLog"].splitlines() if "[emerg]" in line or "[error]" in line or "ready for start up" in line]
        if lines:
            print("Runtime details:")
            for line in lines[-8:]:
                print(line[:300])
    return 0 if result["status"] == "PASS" else (2 if result["status"] in {"BLOCKED", "SKIPPED"} else 1)


if __name__ == "__main__":
    raise SystemExit(main())
