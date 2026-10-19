#!/data/data/com.termux/files/usr/bin/bash
set -uo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || true)"

if [ -z "$PYTHON" ]; then
    printf '[FAIL] Python was not found in PATH. Install/use Python in Termux, then retry.\n' >&2
    exit 1
fi
for file in server.py server_logs.py ui.html chat.py agent.py local_ai.py direct_host.py lan_network.py lan_access.py mdns_advertise.py lan_host.py; do
    if [ ! -f "$ROOT/$file" ]; then
        printf '[FAIL] Required file is missing: %s\n' "$ROOT/$file" >&2
        exit 1
    fi
done
if [ -s "$HOME/.config/cloudflared/token" ] && ! command -v cloudflared >/dev/null 2>&1; then
    printf '[FAIL] A Cloudflare token is configured but cloudflared is not installed. Install it with: pkg install cloudflared\n' >&2
    exit 1
fi
if [ ! -d "$ROOT/shared" ]; then
    printf '[FAIL] Existing shared directory is missing: %s/shared\n' "$ROOT" >&2
    exit 1
fi

cd "$ROOT" || exit 1
if ! "$PYTHON" - <<'PY'
from pathlib import Path
for name in ('server.py', 'server_logs.py', 'chat.py', 'agent.py', 'local_ai.py', 'direct_host.py', 'lan_network.py', 'lan_access.py', 'mdns_advertise.py', 'lan_host.py'):
    compile(Path(name).read_text(encoding='utf-8'), name, 'exec')
print('[OK] Python configuration and source files validated.')
PY
then
    printf '[FAIL] Python source validation failed; no running process was stopped.\n' >&2
    exit 1
fi

"$PYTHON" - <<'PY'
from lan_network import current_wifi_ipv4
try:
    print(f'[INFO] Current Wi-Fi IPv4: {current_wifi_ipv4()}')
except OSError as exc:
    print(f'[WARN] No Wi-Fi IPv4 route yet: {exc}')
PY
printf '[INFO] Starting supervisor. An existing healthy server will be preserved.\n'
printf '[INFO] If server.py is not running, enter its password at the prompt. It will not be written to logs.\n'
printf '[INFO] Intended local URL: http://myserver.local/\n'
printf '[INFO] If configured, Cloudflare Tunnel will be supervised from the private token file.\n'
if command -v tee >/dev/null 2>&1; then
    "$PYTHON" "$ROOT/lan_host.py" --manage-server 2>&1 | tee -a "$ROOT/myserver.log"
    status=${PIPESTATUS[0]}
else
    printf '[WARN] tee is unavailable; logging to myserver.log and the terminal is not possible.\n'
    "$PYTHON" "$ROOT/lan_host.py" --manage-server 2>&1 | cat
    status=${PIPESTATUS[0]}
fi
exit "$status"
