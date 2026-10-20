#!/data/data/com.termux/files/usr/bin/sh
# Idempotent Android boot hook for MyForever.
set -u
umask 077
HOME="${HOME:-/data/data/com.termux/files/home}"
PREFIX="/data/data/com.termux/files/usr"
PATH="$PREFIX/bin:/system/bin:/system/xbin"
TMPDIR="$PREFIX/tmp"
export HOME PREFIX PATH TMPDIR
ROOT="${MYFOREVER_ROOT:-$HOME}"
for candidate in "$HOME/linux_android_device_local_server" "$HOME/myforever"; do
    if [ ! -x "$ROOT/myforever_service.sh" ] && [ -x "$candidate/myforever_service.sh" ]; then ROOT="$candidate"; fi
done
MANAGER="$ROOT/myforever_service.sh"
STATE="$HOME/.local/state/myforever"
LOG="$STATE/supervisor.log"
mkdir -p "$STATE" || exit 1
chmod 700 "$STATE" 2>/dev/null || true
chmod 600 "$LOG" 2>/dev/null || true
# Allow Android's credential-encrypted Termux storage to become available; no network wait.
sleep 20
if [ -x "$MANAGER" ]; then
    "$PREFIX/bin/bash" "$MANAGER" start >> "$LOG" 2>&1
else
    printf '[%s] MyForever manager missing at configured path; no server started.\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" >> "$LOG"
fi
