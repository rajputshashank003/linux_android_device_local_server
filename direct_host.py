#!/usr/bin/env python3
"""Optional direct-domain HTTPS proxy for MyForever; independent of its supervisor/tunnel."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config/myforever/direct-hosting"
SETTINGS = CONFIG_DIR / "settings.json"
CADDYFILE = CONFIG_DIR / "Caddyfile"
CADDY_PID = CONFIG_DIR / "caddy.pid"
CADDY_LOG = CONFIG_DIR / "caddy.log"
GODADDY_AUTH = CONFIG_DIR / "godaddy.json"
DNS_PID = CONFIG_DIR / "godaddy-dns.pid"
DNS_LOG = CONFIG_DIR / "godaddy-dns.log"
DNS_CACHE = CONFIG_DIR / "diagnostics.json"
UPSTREAM = "http://127.0.0.1:8000"
HTTP_PORT = 8080
HTTPS_PORT = 443
DEFAULT_HOST = "direct.costop.in"


def _config():
    try:
        value = json.loads(SETTINGS.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, value):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _hostname_ok(host):
    return (isinstance(host, str) and len(host) <= 253 and len(host.split(".")) >= 3
            and all(1 <= len(x) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", x)
                    for x in host.rstrip(".").split(".")))


def _zone_ok(zone):
    labels = zone.rstrip(".").split(".") if isinstance(zone, str) else []
    return (len(labels) >= 2 and len(zone) <= 253
            and all(1 <= len(x) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", x)
                    for x in labels))


def configure(hostname):
    host = hostname.strip().rstrip(".").lower()
    if not _hostname_ok(host):
        raise ValueError("Use a dedicated hostname such as direct.costop.in.")
    if host in {"www.costop.in", "files.costop.in", "samsung.costop.in"}:
        raise ValueError("That hostname is reserved for an existing service; choose a dedicated subdomain.")
    old = _config()
    if old.get("hostname") and old["hostname"] != host:
        raise RuntimeError("Direct Hosting already has a different hostname configured; review its private settings before changing it.")
    caddy = f"{{\n    http_port {HTTP_PORT}\n}}\n\n{host} {{\n    reverse_proxy 127.0.0.1:8000\n}}\n"
    if CADDYFILE.exists() and CADDYFILE.read_text() != caddy:
        raise RuntimeError("A different Caddyfile already exists; no file was overwritten.")
    old.update({"hostname": host, "upstream": UPSTREAM, "httpPort": HTTP_PORT, "httpsPort": HTTPS_PORT})
    _save(SETTINGS, json.dumps(old, indent=2) + "\n")
    if not CADDYFILE.exists():
        _save(CADDYFILE, caddy)
    return {"hostname": host, "upstream": UPSTREAM, "dnsChanged": False,
            "message": "Prepared a separate Caddy configuration; DNS and existing services were not changed."}


def _get(url, timeout=5):
    request = Request(url, headers={"User-Agent": "MyForever-DirectHosting/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read(65536).decode("utf-8", "replace").strip()


def _public_ip(version):
    url = "https://api.ipify.org" if version == 4 else "https://api64.ipify.org"
    try:
        value = ipaddress.ip_address(_get(url))
        if value.version != version or not value.is_global:
            return None, "no global address returned"
        return str(value), None
    except Exception as exc:
        return None, type(exc).__name__


def _dns(name, kind):
    try:
        url = "https://dns.google/resolve?" + urlencode({"name": name, "type": kind})
        data = json.loads(_get(url))
        answers = [str(x["data"]).rstrip(".") for x in data.get("Answer", []) if x.get("data")]
        return answers
    except Exception:
        return []


def _origin_status():
    try:
        with urlopen(UPSTREAM + "/", timeout=3) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except Exception:
        return None


def _connects(host, port):
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def _port_free(port):
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        s = None
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((address, port))
        except OSError:
            return False
        finally:
            if s:
                s.close()
    return True


def _device_ipv6():
    tool = shutil.which("ifconfig")
    if not tool:
        return []
    try:
        result = subprocess.run([tool, "-a"], capture_output=True, text=True, timeout=3)
        found = set()
        for raw in re.findall(r"\binet6(?:\s+addr:|\s+)([0-9a-fA-F:]+)", result.stdout):
            try:
                address = ipaddress.IPv6Address(raw)
                if address.is_global:
                    found.add(str(address))
            except ValueError:
                pass
        return sorted(found)
    except (OSError, subprocess.TimeoutExpired):
        return []


def _managed_pid(pidfile, expected):
    try:
        pid = int(json.loads(pidfile.read_text()).get("pid", 0))
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        if pid > 1 and expected in cmd:
            os.kill(pid, 0)
            return pid
    except (OSError, ValueError, TypeError, ProcessLookupError):
        pass
    return None


def diagnose():
    cfg = _config()
    host = cfg.get("hostname", "")
    public4, err4 = _public_ip(4)
    public6, err6 = _public_ip(6)
    try:
        from lan_network import current_wifi_ipv4
        local4 = current_wifi_ipv4()
    except Exception as exc:
        local4 = None
        local4_error = type(exc).__name__
    else:
        local4_error = None
    try:
        ip4 = ipaddress.ip_address(local4) if local4 else None
        shared = bool(ip4 and ip4 in ipaddress.ip_network("100.64.0.0/10"))
        private4 = bool(ip4 and ip4.is_private)
    except ValueError:
        shared = private4 = False
    ns = _dns("costop.in", "NS")
    records = {kind: _dns(host, kind) for kind in ("A", "AAAA", "CNAME")} if host else {"A": [], "AAAA": [], "CNAME": []}
    local6 = _device_ipv6()
    origin = _origin_status()
    caddy = shutil.which("caddy")
    caddy_pid = _managed_pid(CADDY_PID, str(CADDYFILE.resolve()))
    godaddy_authoritative = bool(ns) and all(x.lower().endswith("domaincontrol.com") for x in ns)
    dns_match = bool((public4 and public4 in records["A"]) or (public6 and public6 in local6 and public6 in records["AAAA"]))
    if not (public4 or (public6 and local6)):
        result = "DIAGNOSTICS INCOMPLETE"
    elif origin not in (200, 401):
        result = "DIAGNOSTICS INCOMPLETE"
    else:
        result = "DIRECT HOSTING POSSIBLY SUPPORTED, WITH MANUAL NETWORK CONFIGURATION REQUIRED"
    report = {
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "classification": result,
        "localServer": {"url": UPSTREAM, "httpStatus": origin, "responding": origin in (200, 401), "authenticationChallenge": origin == 401},
        "localIPv4": local4, "localIPv4IsPrivate": private4, "localIPv4InSharedCarrierRange": shared,
        "localIPv4DiagnosticError": local4_error, "outboundPublicIPv4": public4, "outboundIPv4LookupError": err4,
        "outboundIPv6Available": bool(public6), "outboundIPv6LookupError": err6,
        "deviceGlobalIPv6": local6, "routerWanAddress": "unavailable; router configuration was not accessible",
        "cgnat": "possible: device uses RFC6598 shared space" if shared else "unknown: router WAN address is unavailable",
        "inboundInternetReachability": "not tested; requires a client outside this network",
        "existingLanPort80Proxy": {"reachableOnCurrentLanAddress": bool(local4 and _connects(local4, 80)),
                                    "127.0.0.1Port80": _connects("127.0.0.1", 80)},
        "portAvailability": {str(p): _port_free(p) for p in (443, HTTP_PORT)},
        "authoritativeNameservers": ns, "godaddyAuthoritative": godaddy_authoritative,
        "dnsHostname": host or None, "dnsRecords": records, "dnsMatchesObservedPublicAddress": dns_match,
        "caddyInstalled": bool(caddy), "caddyExecutable": caddy, "directProxyRunning": bool(caddy_pid),
        "directProxyPid": caddy_pid, "httpsVerified": False,
        "evidenceLimitations": [
            "Outbound public IP lookups do not prove inbound connectivity.",
            "The router WAN IP and port-forwarding/firewall rules are unavailable to this Termux process.",
            "Router NAT versus ISP CGNAT cannot be conclusive without the router WAN address or ISP confirmation.",
            "No test was made from an external device/network, and HTTPS has not been verified.",
        ],
    }
    _save(DNS_CACHE, json.dumps(report, indent=2) + "\n")
    return report


def _godaddy_auth():
    try:
        data = json.loads(GODADDY_AUTH.read_text())
        return data if data.get("key") and data.get("secret") else None
    except (OSError, ValueError, AttributeError):
        return None


def _godaddy_query(method, zone, label, auth, records=None):
    url = f"https://api.godaddy.com/v1/domains/{quote(zone,safe='.-')}/records/A/{quote(label,safe='@.-')}"
    headers = {"Authorization": "sso-key " + auth["key"] + ":" + auth["secret"],
               "Accept": "application/json", "User-Agent": "MyForever-DirectHosting/1.0"}
    data = json.dumps(records).encode() if records is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        try:
            request = Request(url, data=data, method=method, headers=headers)
            with urlopen(request, timeout=10) as response:
                body = response.read(65536)
                return json.loads(body.decode()) if body else []
        except HTTPError as exc:
            if exc.code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"GoDaddy DNS API returned HTTP {exc.code}; details and credentials were withheld.") from None
        except (URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("GoDaddy API request failed after retries.") from None
    raise RuntimeError("GoDaddy API request failed.")


def _godaddy_is_authoritative(zone):
    ns = _dns(zone, "NS")
    return bool(ns) and all(x.lower().endswith("domaincontrol.com") for x in ns)


def configure_godaddy(zone, label, exact_host, interval=300):
    zone = zone.strip().lower().rstrip(".")
    label = label.strip().lower().rstrip(".")
    host = zone if label == "@" else label + "." + zone
    if not _zone_ok(zone) or (label != "@" and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)):
        raise ValueError("Use a DNS zone such as costop.in and a single record label such as direct or @.")
    if exact_host.strip().lower() != host:
        raise ValueError("Type the exact hostname to confirm the DNS target.")
    if interval < 300:
        raise ValueError("Update interval must be at least 300 seconds.")
    if not _godaddy_is_authoritative(zone):
        raise RuntimeError(f"GoDaddy is not authoritative for {zone}; no credentials were requested or saved.")
    key = getpass.getpass("GoDaddy API key (hidden): ").strip()
    secret = getpass.getpass("GoDaddy API secret (hidden): ").strip()
    if not key or not secret:
        raise ValueError("Both API fields are required; nothing was saved.")
    auth = {"key": key, "secret": secret}
    records = _godaddy_query("GET", zone, label, auth)
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise RuntimeError("Expected exactly one existing A record. No DNS change or credential save was performed.")
    if input(f"Existing A record value: {records[0].get('data','unknown')}. Type {host} again to enable DDNS: ").strip().lower() != host:
        raise RuntimeError("Confirmation did not match; nothing was saved.")
    cfg = _config()
    if cfg.get("hostname") != host:
        raise RuntimeError("Configure Direct Hosting to use this hostname first; nothing was saved.")
    _save(GODADDY_AUTH, json.dumps(auth) + "\n")
    cfg.update({"dnsProvider": "godaddy", "dnsZone": zone, "dnsLabel": label,
                "dnsEnabled": True, "dnsIntervalSeconds": interval})
    _save(SETTINGS, json.dumps(cfg, indent=2) + "\n")
    return {"enabled": True, "hostname": host, "intervalSeconds": interval,
            "message": "Explicitly enabled updates for the one reviewed A record; no other records are modified."}


def godaddy_update():
    cfg = _config()
    zone, label = cfg.get("dnsZone"), cfg.get("dnsLabel")
    if not cfg.get("dnsEnabled") or cfg.get("dnsProvider") != "godaddy":
        raise RuntimeError("GoDaddy updates have not been explicitly configured.")
    if not zone or not label or not _godaddy_is_authoritative(zone):
        raise RuntimeError("GoDaddy is not authoritative for this zone; no update was made.")
    auth = _godaddy_auth()
    if not auth:
        raise RuntimeError("Private GoDaddy credentials are unavailable; no update was made.")
    try:
        address = ipaddress.ip_address(_get("https://api.ipify.org"))
        if address.version != 4 or not address.is_global:
            raise ValueError
    except Exception:
        raise RuntimeError("No global public IPv4 was detected; no DNS update was made.") from None
    records = _godaddy_query("GET", zone, label, auth)
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise RuntimeError("The target A record is missing or ambiguous; no update was made.")
    if records[0].get("data") == str(address):
        updated = False
    else:
        allowed = {k: v for k, v in records[0].items() if k in ("data", "ttl", "name", "type")}
        allowed["data"] = str(address)
        _godaddy_query("PUT", zone, label, auth, [allowed])
        check = _godaddy_query("GET", zone, label, auth)
        if not isinstance(check, list) or len(check) != 1 or check[0].get("data") != str(address):
            raise RuntimeError("DNS update verification failed.")
        updated = True
    cfg["lastDnsUpdate"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    cfg["lastDnsAddress"] = str(address)
    cfg.pop("lastDnsError", None)
    _save(SETTINGS, json.dumps(cfg, indent=2) + "\n")
    return {"updated": updated, "address": str(address), "message": "GoDaddy A record matches the detected public IPv4."}


def _dns_watch():
    while True:
        try:
            result = godaddy_update()
            message = result["message"]
            cfg = _config(); cfg.pop("lastDnsError", None)
            _save(SETTINGS, json.dumps(cfg, indent=2) + "\n")
        except Exception as exc:
            cfg = _config(); cfg["lastDnsError"] = str(exc)[:250]
            _save(SETTINGS, json.dumps(cfg, indent=2) + "\n")
            message = "DNS update failed: " + str(exc)[:250]
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with DNS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + message + "\n")
        time.sleep(max(300, int(_config().get("dnsIntervalSeconds", 300))))


def _start_caddy(confirmation):
    cfg = _config(); host = cfg.get("hostname")
    if not host or confirmation != host:
        raise RuntimeError("Type the configured hostname exactly to confirm starting the direct service.")
    caddy = shutil.which("caddy")
    if not caddy:
        raise RuntimeError("Caddy is not installed. Termux provides it with: pkg install caddy")
    pid = _managed_pid(CADDY_PID, str(CADDYFILE.resolve()))
    if pid:
        return {"running": True, "pid": pid, "message": "Managed direct Caddy service is already running."}
    report = diagnose()
    if not report["localServer"]["responding"]:
        raise RuntimeError("Existing MyForever server at port 8000 did not respond; proxy not started.")
    if not report["dnsMatchesObservedPublicAddress"]:
        raise RuntimeError("Dedicated DNS A/AAAA record does not match the current public address; no proxy was started and DNS was not changed.")
    conflicts = [port for port in (443, HTTP_PORT) if not _port_free(port)]
    if conflicts:
        raise RuntimeError("Direct service ports in use: " + ", ".join(map(str, conflicts)) + "; no process was stopped.")
    result = subprocess.run([caddy, "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Caddy config validation failed; existing services were untouched.")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log = CADDY_LOG.open("ab", buffering=0)
    try:
        proc = subprocess.Popen([caddy, "run", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
                                cwd=str(CONFIG_DIR), stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    finally:
        log.close()
    time.sleep(.8)
    if proc.poll() is not None:
        raise RuntimeError("Caddy exited at startup; inspect its log. MyForever and Cloudflare were not stopped.")
    _save(CADDY_PID, json.dumps({"pid": proc.pid}) + "\n")
    return {"running": True, "pid": proc.pid, "message": "Caddy started; public reachability and HTTPS still require external verification."}


def _stop_pid(pidfile, expected):
    pid = _managed_pid(pidfile, expected)
    if not pid:
        try: pidfile.unlink()
        except OSError: pass
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        try: os.kill(pid, 0)
        except ProcessLookupError: break
        time.sleep(.1)
    else: raise RuntimeError("Managed process did not stop within five seconds.")
    try: pidfile.unlink()
    except OSError: pass
    return True


def start_dns_watch():
    cfg = _config()
    if not cfg.get("dnsEnabled") or not _godaddy_is_authoritative(cfg.get("dnsZone", "")):
        raise RuntimeError("GoDaddy DNS updates are not explicitly enabled for an authoritative zone.")
    expected = str(Path(__file__).resolve())
    pid = _managed_pid(DNS_PID, expected)
    if pid:
        return {"running": True, "pid": pid}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log = DNS_LOG.open("ab", buffering=0)
    try:
        proc = subprocess.Popen([sys.executable, expected, "dns-watch"], cwd=str(CONFIG_DIR),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    finally:
        log.close()
    time.sleep(.3)
    if proc.poll() is not None:
        raise RuntimeError("GoDaddy DNS updater exited during startup.")
    _save(DNS_PID, json.dumps({"pid": proc.pid}) + "\n")
    return {"running": True, "pid": proc.pid}


def stop_dns_watch():
    return {"running": False, "stopped": _stop_pid(DNS_PID, str(Path(__file__).resolve()))}


def status():
    cfg = _config()
    try:
        report = json.loads(DNS_CACHE.read_text())
        if not isinstance(report, dict): report = {}
    except (OSError, ValueError): report = {}
    origin = _origin_status()
    report.update({"configuredHostname": cfg.get("hostname"), "localServer": {
        "url": UPSTREAM, "httpStatus": origin, "responding": origin in (200, 401),
        "authenticationChallenge": origin == 401}, "caddyInstalled": bool(shutil.which("caddy")),
        "directProxyRunning": bool(_managed_pid(CADDY_PID, str(CADDYFILE.resolve()))),
        "goDaddyDnsUpdaterRunning": bool(_managed_pid(DNS_PID, str(Path(__file__).resolve()))),
        "dnsUpdater": {"provider": cfg.get("dnsProvider", "manual"), "enabled": bool(cfg.get("dnsEnabled")),
                       "credentialsStored": bool(_godaddy_auth()), "lastUpdate": cfg.get("lastDnsUpdate"),
                       "lastError": cfg.get("lastDnsError")}})
    return report


def logs():
    out = {}
    for key, path in (("proxy", CADDY_LOG), ("dns", DNS_LOG)):
        try: lines = path.read_text(errors="replace").splitlines()[-80:]
        except OSError: lines = []
        out[key] = [re.sub(r'(?i)(authorization|api[_-]?key|secret|token)(["\s]*[:=]["\s]*)[^\s,}\"]+', r"\1\2[redacted]", line)[:1000] for line in lines]
    return out


def main():
    parser = argparse.ArgumentParser(description="Independent optional Direct Domain Hosting service")
    subs = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("diagnose", "status", "stop", "logs", "update-dns", "start-dns", "stop-dns", "dns-watch"):
        subs.add_parser(cmd)
    conf = subs.add_parser("configure"); conf.add_argument("hostname", nargs="?", default=DEFAULT_HOST)
    start = subs.add_parser("start"); start.add_argument("--confirm-host", required=True)
    dns = subs.add_parser("configure-godaddy"); dns.add_argument("zone"); dns.add_argument("label"); dns.add_argument("--confirm-host", required=True); dns.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    try:
        if args.cmd == "diagnose": result = diagnose()
        elif args.cmd == "status": result = status()
        elif args.cmd == "configure": result = configure(args.hostname)
        elif args.cmd == "start": result = _start_caddy(args.confirm_host)
        elif args.cmd == "stop": result = {"running": False, "stopped": _stop_pid(CADDY_PID, str(CADDYFILE.resolve())), "message": "Only the Direct Hosting Caddy process was targeted."}
        elif args.cmd == "logs": result = logs()
        elif args.cmd == "update-dns": result = godaddy_update()
        elif args.cmd == "configure-godaddy": result = configure_godaddy(args.zone,args.label,args.confirm_host,args.interval)
        elif args.cmd == "start-dns": result = start_dns_watch()
        elif args.cmd == "stop-dns": result = stop_dns_watch()
        elif args.cmd == "dns-watch": _dns_watch(); return
        else: raise RuntimeError("Unknown command.")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
