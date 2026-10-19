#!/data/data/com.termux/files/usr/bin/bash
# Manage only the lan_host.py supervisor and its own managed children.
set -u
umask 077
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || true)"
STATE="$HOME/.local/state/myforever"
CONFIG="$HOME/.config/myforever"
LOG="$STATE/supervisor.log"
PIDFILE="$STATE/supervisor.pid"
PASSFILE="$CONFIG/server_password"
mkdir -p "$STATE"
chmod 700 "$STATE" 2>/dev/null || true

usage() {
    cat <<'HELP'
Usage: bash myforever_service.sh COMMAND
  configure-password  Save the existing/desired server password privately for unattended starts
  start               Start one detached MyForever/LAN/Cloudflare supervisor
  stop                Stop that supervisor and only the children it owns
  restart             Restart its managed server child (does not restart the tunnel)
  status              Check supervisor, authenticated server response, and tunnel metrics
  logs                Follow the supervisor log
  diagnose            Run the existing diagnostic script
HELP
}

verified_supervisor() {
    [ -r "$PIDFILE" ] || return 1
    local pid cmd
    IFS= read -r pid < "$PIDFILE" || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [ -r "/proc/$pid/cmdline" ] || return 1
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmd" in
        *"$ROOT/lan_host.py"*"--manage-server"*) printf '%s' "$pid"; return 0 ;;
        *) return 1 ;;
    esac
}

rotate_log() {
    [ -f "$LOG" ] || return 0
    local bytes
    bytes="$(wc -c < "$LOG" 2>/dev/null || printf '0')"
    if [ "${bytes//[!0-9]/}" -ge 2097152 ] 2>/dev/null; then
        [ ! -e "$LOG.2" ] || mv -f "$LOG.2" "$LOG.3"
        [ ! -e "$LOG.1" ] || mv -f "$LOG.1" "$LOG.2"
        mv -f "$LOG" "$LOG.1"
    fi
}

probe_server() {
    "$PYTHON" - <<'PY'
import http.client, sys
try:
 c=http.client.HTTPConnection('127.0.0.1',8000,timeout=2); c.request('GET','/')
 r=c.getresponse(); status=r.status; server=r.getheader('Server',''); r.read(256); c.close()
 if 'TermuxFileServer' not in server: print('wrong-service'); sys.exit(2)
 print(status)
except Exception: sys.exit(1)
PY
}

case "${1:-}" in
    configure-password)
        [ ! -e "$PASSFILE" ] || { printf 'Refusing to overwrite the existing private password file.\n' >&2; exit 1; }
        mkdir -p "$CONFIG" && chmod 700 "$CONFIG"
        "$PYTHON" - "$PASSFILE" <<'PY'
import getpass, os, sys
path=sys.argv[1]
a=getpass.getpass('Enter the MyForever password to retain for unattended restarts: ')
b=getpass.getpass('Confirm password: ')
if not a or a != b:
 print('Passwords are empty or do not match; no file written.', file=sys.stderr); sys.exit(1)
fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
try:
 os.write(fd, a.encode('utf-8'))
 os.fsync(fd)
finally: os.close(fd)
print('Private password file created. The password was not displayed or logged.')
PY
        ;;
    start)
        for file in server.py lan_host.py lan_access.py mdns_advertise.py; do
            [ -f "$ROOT/$file" ] || { printf 'Required MyForever file is missing: %s\n' "$file" >&2; exit 1; }
        done
        [ -d "$ROOT/shared" ] || { printf 'Shared file directory is missing; refusing startup.\n' >&2; exit 1; }
        if pid="$(verified_supervisor)"; then
            printf 'MyForever supervisor is already running (pid %s). No duplicate started.\n' "$pid"
            exit 0
        fi
        [ -s "$PASSFILE" ] || { printf 'Unattended start requires a private password file. Run: bash myforever_service.sh configure-password\n' >&2; exit 1; }
        [ -f "$ROOT/lan_host.py" ] || { printf 'lan_host.py is missing.\n' >&2; exit 1; }
        [ -x "$PYTHON" ] || { printf 'Python was not found.\n' >&2; exit 1; }
        if command -v termux-wake-lock >/dev/null 2>&1; then
            termux-wake-lock >/dev/null 2>&1 || printf '[%s] wake-lock request failed or Termux:API companion unavailable\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" >> "$LOG"
        fi
        rotate_log
        printf '[%s] starting supervisor\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" >> "$LOG"
        chmod 600 "$LOG" 2>/dev/null || true
        cd "$ROOT" || exit 1
        nohup env TERMUX_SERVER_PASSWORD_FILE="$PASSFILE" "$PYTHON" -u "$ROOT/lan_host.py" --manage-server >> "$LOG" 2>&1 </dev/null &
        pid=$!
        (umask 077; printf '%s\n' "$pid" > "$PIDFILE")
        sleep 2
        if verified_supervisor >/dev/null; then
            printf 'Started MyForever supervisor (pid %s). Check: bash myforever_service.sh status\n' "$pid"
        else
            printf 'Supervisor did not remain running. Inspect: bash myforever_service.sh logs\n' >&2
            exit 1
        fi
        ;;
    stop)
        if ! pid="$(verified_supervisor)"; then
            printf 'No verified MyForever supervisor is running. Existing unmanaged processes were left untouched.\n'
            exit 0
        fi
        kill -TERM "$pid" 2>/dev/null || { printf 'Could not signal verified supervisor pid %s.\n' "$pid" >&2; exit 1; }
        for _ in $(seq 1 15); do
            [ -r "/proc/$pid/cmdline" ] || break
            sleep 1
        done
        if [ -r "/proc/$pid/cmdline" ]; then
            printf 'Supervisor is still stopping (pid %s); inspect logs before taking further action.\n' "$pid"
        else
            printf 'Supervisor stopped; only children it created are stopped.\n'
            command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock >/dev/null 2>&1 || true
        fi
        ;;
    restart)
        if pid="$(verified_supervisor)"; then
            if [ -r "/proc/$pid/cmdline" ] && kill -0 "$pid" 2>/dev/null; then
                kill -USR1 "$pid" 2>/dev/null || { printf 'Server restart signal failed.\n' >&2; exit 1; }
                printf 'Sent a request to restart the supervisor-managed server only; check logs for confirmation. The tunnel and proxy are not intentionally restarted.\n'
            fi
        else
            printf 'No verified supervisor is running. Starting the supervised deployment.\n'
            exec "$0" start
        fi
        ;;
    status)
        if pid="$(verified_supervisor)"; then printf 'Supervisor: running (pid %s)\n' "$pid"; else printf 'Supervisor: stopped\n'; fi
        if [ -n "$PYTHON" ]; then
            result="$(probe_server 2>/dev/null)"; rc=$?
            if [ "$rc" -eq 0 ]; then
                case "$result" in 401) printf 'MyForever port 8000: responding; HTTP 401 is the expected unauthenticated password challenge.\n' ;; *) printf 'MyForever port 8000: responding (HTTP %s).\n' "$result" ;; esac
            else printf 'MyForever port 8000: not responding as the expected service.\n'; fi
        fi
        "$PYTHON" - <<'PY'
import http.client
try:
 c=http.client.HTTPConnection('127.0.0.1',20241,timeout=1); c.request('GET','/metrics'); r=c.getresponse(); body=r.read(128000).decode('utf-8','replace'); c.close()
 count=next((line.rsplit(' ',1)[1] for line in body.splitlines() if line.startswith('cloudflared_tunnel_ha_connections ')),None)
 print('Cloudflare Tunnel: '+(f'metrics active edges={count}' if r.status==200 and count is not None else 'metrics endpoint unavailable'))
except Exception: print('Cloudflare Tunnel: metrics endpoint unavailable (not proof that no connector exists)')
PY
        ;;
    logs)
        if command -v tail >/dev/null 2>&1; then tail -n 100 -F "$LOG"; else cat "$LOG"; fi
        ;;
    diagnose)
        exec bash "$ROOT/diagnose_myserver.sh"
        ;;
    *) usage; exit 2 ;;
esac
