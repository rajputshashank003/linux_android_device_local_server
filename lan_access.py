#!/usr/bin/env python3
"""LAN-only port-80 reverse proxy to the existing Termux server on port 8000."""
import http.client
import ipaddress
import os
import socket

from lan_network import current_wifi_ipv4
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UPSTREAM = ("127.0.0.1", 8000)
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade"}


def local_ipv4():
    return current_wifi_ipv4()



class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TermuxLANProxy/1.0"
    sys_version = ""

    def handle_request(self):
        route = urlsplit(self.path)
        if route.scheme or route.netloc:
            self.send_error(400, "Only local origin-form requests are accepted")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(411, "Content-Length required")
            return
        if length < 0:
            self.send_error(400, "Invalid Content-Length")
            return
        try:
            upstream = http.client.HTTPConnection(*UPSTREAM, timeout=60)
            upstream.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for key, value in self.headers.items():
                if key.lower() not in HOP_HEADERS and key.lower() not in ("host", "content-length"):
                    upstream.putheader(key, value)
            if self.headers.get("Content-Length") is not None:
                upstream.putheader("Content-Length", str(length))
            upstream.putheader("Host", f"127.0.0.1:{UPSTREAM[1]}")
            upstream.putheader("Connection", "close")
            upstream.endheaders()
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ConnectionError("incomplete client request")
                upstream.send(chunk)
                remaining -= len(chunk)
            response = upstream.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS and key.lower() not in ("server", "date"):
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            upstream.close()
            self.close_connection = True
        except (OSError, http.client.HTTPException, ConnectionError) as exc:
            self.send_error(502, f"Backend unavailable: {exc}")

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_DELETE = handle_request
    do_OPTIONS = handle_request
    do_PATCH = handle_request

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}", flush=True)


def main():
    address = os.environ.get("TERMUX_LAN_BIND_ADDRESS") or local_ipv4()
    try:
        parsed = ipaddress.ip_address(address)
        if parsed.version != 4 or not parsed.is_private or parsed.is_loopback or parsed.is_link_local:
            raise ValueError
    except ValueError:
        raise SystemExit(f"Refusing non-private or invalid bind address: {address}")
    try:
        server = ThreadingHTTPServer((address, 80), ProxyHandler)
    except OSError as exc:
        raise SystemExit(f"Could not bind port 80 on {address}: {exc}")
    print(f"Proxying http://{address}/ to http://127.0.0.1:8000", flush=True)
    print("Use the IP URL on devices connected to the same Wi-Fi.", flush=True)
    print("For myserver.local advertisement, run lan_host.py with mdns_advertise.py beside this proxy.", flush=True)
    print("The proxy is bound only to this private Wi-Fi address. Ctrl+C stops the proxy; the file server stays running.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Proxy stopped; the existing file server was not stopped.", flush=True)


if __name__ == "__main__":
    main()
