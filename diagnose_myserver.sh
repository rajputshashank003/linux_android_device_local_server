#!/data/data/com.termux/files/usr/bin/bash
set -u
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || true)"

printf 'myserver diagnostics\nProject: %s\n\n' "$ROOT"
for file in server.py ui.html lan_network.py lan_access.py mdns_advertise.py lan_host.py db_explorer.py start_myserver.sh configure_cloudflared_token.sh; do
    if [ -f "$ROOT/$file" ]; then printf '[OK] file exists: %s\n' "$file"; else printf '[FAIL] missing file: %s\n' "$file"; fi
done
[ -d "$ROOT/shared" ] && printf '[OK] shared directory exists\n' || printf '[FAIL] shared directory is missing\n'

if [ -z "$PYTHON" ]; then
    printf '\n[FAIL] Python was not found in PATH.\n'
    exit 1
fi

cd "$ROOT" || exit 1
"$PYTHON" - <<'PY'
import http.client
import ipaddress
import os
import re
import socket
import struct
import subprocess
import time

print('\nNetwork address')
try:
    from lan_network import current_wifi_ipv4, _ifconfig_addresses
    address = current_wifi_ipv4()
    entries = _ifconfig_addresses()
    interface = next((e['name'] for e in entries if e['address'] == address), 'route-selected interface')
    print(f'[OK] Wi-Fi IPv4: {address} ({interface})')
except Exception as exc:
    address = None
    print(f'[FAIL] Could not determine Wi-Fi IPv4: {exc}')

print('\nProcess checks')
try:
    ps = subprocess.run(['ps', '-ef'], capture_output=True, text=True, timeout=3)
    lines = ps.stdout.splitlines()
    for label, needles in (
        ('file server', ('server.py',)),
        ('supervisor', ('lan_host.py',)),
        ('proxy', ('lan_access.py',)),
        ('mDNS responder', ('mdns_advertise.py',)),
        ('Cloudflare connector', ('cloudflared tunnel', 'run', '--token-file')),
    ):
        matches = [line.strip() for line in lines if any(n in line for n in needles)
                   and '/bin/sh -c' not in line and 'grep -E' not in line and 'diagnose_myserver.sh' not in line]
        print((f'[OK] {label}: ' + '; '.join(matches)) if matches else f'[FAIL] {label} process not found')
except Exception as exc:
    print(f'[WARN] ps process listing unavailable: {exc}')

print('\nHTTP and TCP checks')
def http_check(host, port, label, expected_server):
    try:
        conn = http.client.HTTPConnection(host, port, timeout=3)
        conn.request('GET', '/')
        response = conn.getresponse()
        server = response.getheader('Server', '')
        status = response.status
        response.read(1024)
        conn.close()
        if expected_server in server:
            print(f'[OK] {label}: HTTP {status}, Server={server!r} (401 is expected without a password)')
            return True
        print(f'[FAIL] {label}: port answered HTTP {status}, but Server={server!r}')
    except Exception as exc:
        print(f'[FAIL] {label}: {exc}')
    return False

http_check('127.0.0.1', 8000, 'localhost:8000', 'TermuxFileServer')
if address:
    http_check(address, 8000, f'{address}:8000 (LAN bind)', 'TermuxFileServer')
    http_check(address, 80, f'{address}:80 proxy', 'TermuxLANProxy')
http_check('myserver.local', 80, 'http://myserver.local (hostname HTTP)', 'TermuxLANProxy')

print('\nCloudflare Tunnel check')
try:
    import urllib.request
    with urllib.request.urlopen('http://127.0.0.1:20241/metrics', timeout=2) as response:
        metrics = response.read(128000).decode('utf-8', 'replace')
    count = next((int(float(line.rsplit(' ', 1)[1])) for line in metrics.splitlines()
                  if line.startswith('cloudflared_tunnel_ha_connections ')), None)
    if count is None:
        print('[WARN] cloudflared metrics endpoint responded, but active connection count was unavailable')
    elif count > 0:
        print(f'[OK] Cloudflare connector has {count} active edge connections')
    else:
        print('[FAIL] Cloudflare metrics reports zero active edge connections')
except Exception as exc:
    print(f'[WARN] Cloudflare metrics endpoint unavailable; connector is not verified connected: {exc}')

print('\nListener inspection')
found_listener_tool = False
for cmd in (['ss', '-ltnup'], ['netstat', '-lntup']):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        continue
    found_listener_tool = True
    combined = (result.stderr + '\n' + result.stdout).strip()
    if result.returncode == 0 and result.stdout.strip() and not any(msg in combined.lower() for msg in ('no support', 'permission denied', 'cannot open netlink')):
        print(result.stdout.rstrip())
        break
    detail = combined.splitlines()
    print(f'[WARN] {cmd[0]} cannot inspect sockets here: {detail[0] if detail else "permission denied"}')
if not found_listener_tool:
    print('[WARN] Neither ss nor netstat is available; HTTP connect checks above verify reachable TCP listeners.')
print('[INFO] Android denied netlink/proc socket inspection where reported; no root commands were used.')

print('\nLocal hostname check')
try:
    resolved = sorted({item[4][0] for item in socket.getaddrinfo('myserver.local', 80, socket.AF_INET, socket.SOCK_STREAM)})
    print(f'[OK] system resolver: myserver.local -> {", ".join(resolved)}')
    if address and address not in resolved:
        print(f'[FAIL] resolver address does not match current Wi-Fi IPv4 {address}')
except OSError as exc:
    resolved = []
    print(f'[FAIL] system resolver: {exc}')

print('\nActive mDNS query (legacy-unicast RFC 6762 query from an ephemeral source port)')
if address:
    qid = int.from_bytes(os.urandom(2), 'big')
    qname = b''.join(bytes([len(label)]) + label.encode('ascii') for label in 'myserver.local'.split('.')) + b'\0'
    question = qname + struct.pack('!HH', 1, 1)
    query = struct.pack('!HHHHHH', qid, 0, 1, 0, 0, 0) + question
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(2.5)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.sendto(query, ('224.0.0.251', 5353))
        packet, peer = sock.recvfrom(4096)
        ident, flags, qd, an, _ns, _ar = struct.unpack_from('!HHHHHH', packet, 0)
        valid = (ident == qid and flags & 0x8400 == 0x8400 and qd == 1 and an >= 1
                 and packet.endswith(socket.inet_aton(address)))
        if valid:
            print(f'[OK] query answered by {peer[0]}:{peer[1]} with authoritative A {socket.inet_ntoa(packet[-4:])}')
        else:
            print(f'[FAIL] received a packet from {peer}, but it did not contain the expected valid A response (id={ident}, flags=0x{flags:04x}, answers={an})')
    except Exception as exc:
        print(f'[FAIL] no valid mDNS answer: {exc}')
    finally:
        sock.close()

    print('Standard multicast query from UDP 5353')
    mdns_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        mdns_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            try: mdns_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError: pass
        mdns_sock.bind(('', 5353))
        mdns_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton('224.0.0.251') + socket.inet_aton(address))
        mdns_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        mdns_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        mdns_sock.settimeout(2)
        standard_query = struct.pack('!HHHHHH', 0, 0, 1, 0, 0, 0) + question
        mdns_sock.sendto(standard_query, ('224.0.0.251', 5353))
        valid = False
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            mdns_sock.settimeout(max(0.1, deadline - time.monotonic()))
            packet, peer = mdns_sock.recvfrom(4096)
            ident, flags, _qd, an, _ns, _ar = struct.unpack_from('!HHHHHH', packet, 0)
            if ident == 0 and flags & 0x8400 == 0x8400 and an >= 1 and packet.endswith(socket.inet_aton(address)):
                valid = True
                print(f'[OK] multicast response from {peer[0]}:5353; authoritative A={socket.inet_ntoa(packet[-4:])}')
                break
        if not valid:
            print('[FAIL] no valid multicast response arrived on UDP 5353')
    except Exception as exc:
        print(f'[WARN] Could not perform port-5353 multicast query: {exc}')
    finally:
        mdns_sock.close()
else:
    print('[SKIP] no private Wi-Fi address was detected')

print('\nAndroid/system limitations')
for command in (['ip', 'route'], ['route', '-n']):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
        if result.returncode == 0 and result.stdout.strip():
            print(f'[INFO] {command[0]} route output available')
            break
        message = (result.stderr or result.stdout).strip().splitlines()
        if message:
            print(f'[INFO] {command[0]} unavailable: {message[0]}')
    except (OSError, subprocess.TimeoutExpired):
        continue
PY

printf '\nRecent logs\n'
for log in "$ROOT/myserver.log" "$ROOT/lan_host.log" "$ROOT/cloudflared.log"; do
    if [ -f "$log" ]; then
        printf '\n--- %s ---\n' "${log##*/}"
        tail -n 30 "$log" 2>/dev/null || printf '[WARN] log could not be read\n'
    else
        printf '[INFO] no log file: %s\n' "${log##*/}"
    fi
done
printf '\nIf the hostname works but a client cannot open it, compare http://myserver.local/ with the IP URL printed above. Same-Wi-Fi client isolation and client-side .local support cannot be changed by Termux.\n'
