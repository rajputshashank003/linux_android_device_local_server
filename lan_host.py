#!/usr/bin/env python3
"""Supervise the existing Termux file server, LAN proxy, and mDNS responder."""
import argparse
import fcntl
import http.client
import os
from pathlib import Path
import signal
import shutil
import struct
import socket
import subprocess
import sys
import tempfile
import time

from lan_network import current_wifi_ipv4

ROOT = Path(__file__).resolve().parent
POLL_SECONDS = 3
NETWORK_GRACE_SECONDS = 12
SERVER_START_TIMEOUT = 120
PROXY_RETRY_SECONDS = 8
MDNS_RETRY_SECONDS = 30
TUNNEL_RETRY_SECONDS = 20
TOKEN_FILE = Path.home() / ".config" / "cloudflared" / "token"
TUNNEL_LOG = ROOT / "cloudflared.log"


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


def ensure_server(manage_server):
    status = server_response()
    if status is not None:
        print(f"Existing file server is responding on 127.0.0.1:8000 (HTTP {status}); leaving it running.", flush=True)
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
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


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


def start_proxy(address):
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


def start_mdns():
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


def other_tunnel_process_exists():
    """Avoid starting a second connector that is not owned by this supervisor."""
    try:
        result = subprocess.run(["ps", "-ef"], capture_output=True, text=True, timeout=3)
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) >= 3 and "cloudflared tunnel run" in fields[2]:
                try:
                    if int(fields[1]) != os.getpid():
                        return True
                except ValueError:
                    continue
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_tunnel():
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
    tunnel_reported_connected = False
    tunnel_config_notice = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        server = ensure_server(args.manage_server)
        owns_server = server is not None
        print("LAN supervisor active. Press Ctrl+C to stop its children.", flush=True)
        while not stopping:
            now = time.monotonic()
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
                bound_address = routed
                proxy_retry_at = now
                if routed:
                    proxy = start_proxy(routed)
                    if proxy is None:
                        proxy_retry_at = now + PROXY_RETRY_SECONDS
                else:
                    print("Proxy is waiting for a private Wi-Fi IPv4 address.", flush=True)

            if mdns is None and now >= mdns_retry_at:
                mdns = start_mdns()
                if mdns is None:
                    mdns_retry_at = now + MDNS_RETRY_SECONDS
            elif mdns is not None and mdns.poll() is not None:
                code = mdns.returncode
                mdns = None
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
                if tunnel is None and now >= tunnel_retry_at:
                    tunnel = start_tunnel()
                    if tunnel is None:
                        tunnel_retry_at = now + TUNNEL_RETRY_SECONDS
                elif tunnel is not None and tunnel.poll() is not None:
                    code = tunnel.returncode
                    tunnel = None
                    tunnel_retry_at = now + TUNNEL_RETRY_SECONDS
                    tunnel_reported_connected = False
                    print(f"cloudflared exited (status {code}); retrying in {TUNNEL_RETRY_SECONDS}s.", flush=True)
                elif tunnel is not None:
                    connections = tunnel_connections()
                    if connections and not tunnel_reported_connected:
                        print(f"Cloudflare tunnel connected ({connections} active edge connections).", flush=True)
                        tunnel_reported_connected = True
                    elif connections == 0 and tunnel_reported_connected:
                        print("Cloudflare tunnel currently has no active edge connections.", flush=True)
                        tunnel_reported_connected = False
            elif not tunnel_config_notice:
                print(f"Cloudflare connector is not configured; add a private token file at {TOKEN_FILE} to enable it.", flush=True)
                tunnel_config_notice = True

            if bound_address and proxy is None and now >= proxy_retry_at:
                proxy = start_proxy(bound_address)
                if proxy is None:
                    proxy_retry_at = now + PROXY_RETRY_SECONDS
            elif proxy is not None and proxy.poll() is not None:
                code = proxy.returncode
                proxy = None
                proxy_retry_at = now + PROXY_RETRY_SECONDS
                print(f"Port-80 proxy exited (status {code}); retrying in {PROXY_RETRY_SECONDS}s.", flush=True)

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
            print("Stopped the file server started by this supervisor.", flush=True)
        else:
            print("Left the pre-existing file server running.", flush=True)
        lock.close()
        if stopping:
            print("LAN supervisor stopped cleanly.", flush=True)


if __name__ == "__main__":
    main()
