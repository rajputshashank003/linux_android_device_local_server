#!/usr/bin/env python3
"""Development supervisor: restart only the app worker when source changes."""
import argparse
import fcntl
import getpass
import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
WATCHED = (ROOT / "server.py", ROOT / "ui.html")
DEBOUNCE_SECONDS = 0.8
POLL_SECONDS = 0.3


def source_signature():
    digest = hashlib.sha256()
    for path in WATCHED:
        try:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.digest()


def ensure_port_free(port):
    probe = socket.socket()
    try:
        probe.bind(("0.0.0.0", port))
    except OSError as exc:
        raise SystemExit(f"Port {port} is already occupied. Stop the existing server before starting dev.py: {exc}")
    finally:
        probe.close()


def stop_worker(worker):
    if worker is None or worker.poll() is not None:
        return
    worker.terminate()
    try:
        worker.wait(timeout=4)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser(description="Run the file server with development source watching.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    lock_path = Path(tempfile.gettempdir()) / f"termux-file-server-dev-{os.getuid()}.lock"
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another dev.py supervisor is already running.")
    ensure_port_free(args.port)
    password = os.environ.get("TERMUX_SERVER_PASSWORD") or getpass.getpass("File server password: ")
    if not password:
        raise SystemExit("Password cannot be empty.")
    env = os.environ.copy()
    env["TERMUX_SERVER_PASSWORD"] = password
    env["PYTHONUNBUFFERED"] = "1"
    signature = source_signature()
    worker = None
    worker_started = 0.0
    crash_delay = 1.0
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def launch():
        nonlocal worker_started
        worker = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(args.port)],
                                  cwd=ROOT, env=env, bufsize=0)
        worker_started = time.monotonic()
        print(f"Dev worker started (pid {worker.pid}, port {args.port}).", flush=True)
        return worker

    print("Watching server.py and ui.html. Changes under shared/ are ignored.", flush=True)
    worker = launch()
    pending_since = None
    last_signature = signature
    try:
        while not stopping:
            time.sleep(POLL_SECONDS)
            now = time.monotonic()
            current = source_signature()
            if current != last_signature:
                last_signature = current
                pending_since = now
            if pending_since is not None and now - pending_since >= DEBOUNCE_SECONDS:
                print("Source change detected; restarting app worker.", flush=True)
                stop_worker(worker)
                worker = launch()
                signature = last_signature
                pending_since = None
                continue
            if worker is not None and worker.poll() is None and now - worker_started >= 10:
                crash_delay = 1.0
            if worker is not None and worker.poll() is not None:
                code = worker.returncode
                print(f"App worker exited ({code}); restarting in {crash_delay:.1f}s.", flush=True)
                time.sleep(crash_delay)
                if not stopping:
                    crash_delay = min(crash_delay * 2, 30.0)
                    worker = launch()
    except KeyboardInterrupt:
        stopping = True
    finally:
        stop_worker(worker)
        print("Dev supervisor stopped.", flush=True)
        lock.close()


if __name__ == "__main__":
    main()
