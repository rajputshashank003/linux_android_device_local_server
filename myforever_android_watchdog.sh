#!/data/data/com.termux/files/usr/bin/sh
# Android JobScheduler fallback: only start MyForever if its verified supervisor is absent.
set -u
umask 077
HOME="${HOME:-/data/data/com.termux/files/home}"
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
PATH="$PREFIX/bin:/system/bin:/system/xbin"
export HOME PREFIX PATH
ROOT="${MYFOREVER_ROOT:-$HOME}"
MANAGER="$ROOT/myforever_service.sh"
STATE="$HOME/.local/state/myforever"
LOG="$STATE/android-watchdog.log"
mkdir -p "$STATE" || exit 1
chmod 700 "$STATE" 2>/dev/null || true
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || printf 0)" -gt 262144 ]; then
    tail -c 65536 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    chmod 600 "$LOG" 2>/dev/null || true
fi
exec >> "$LOG" 2>&1
printf '[%s] Android scheduled recovery check started.\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
if [ ! -x "$MANAGER" ]; then
    printf '[%s] Manager missing or not executable; no process action taken.\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
    exit 1
fi
"$PREFIX/bin/bash" "$MANAGER" start
