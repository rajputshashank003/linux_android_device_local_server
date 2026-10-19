#!/data/data/com.termux/files/usr/bin/sh
# Termux boot hook for MyForever. The service manager acquires wake lock after validation.
umask 077
HOME="${HOME:-/data/data/com.termux/files/home}"
ROOT="${MYFOREVER_ROOT:-$HOME}"
if [ ! -x "$ROOT/myforever_service.sh" ]; then
    for candidate in "$HOME/linux_android_device_local_server" "$HOME/myforever"; do
        if [ -x "$candidate/myforever_service.sh" ]; then ROOT="$candidate"; break; fi
    done
fi
MANAGER="$ROOT/myforever_service.sh"
STATE="$HOME/.local/state/myforever"
LOG="$STATE/supervisor.log"
mkdir -p "$STATE"
sleep 20
if [ -x "$MANAGER" ]; then
    bash "$MANAGER" start >> "$LOG" 2>&1
else
    printf '[%s] MyForever manager not found. Set MYFOREVER_ROOT or install the project at the expected path.\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" >> "$LOG"
fi
