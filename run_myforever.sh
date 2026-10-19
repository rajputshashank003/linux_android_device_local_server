#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="$(command -v python3 || command -v python || true)"

if [[ -z "$PYTHON" ]]; then
    printf '[FAIL] Python 3 is required. Install Python for your platform, then retry.\n' >&2
    exit 1
fi

for file in server.py server_logs.py ui.html chat.py agent.py local_ai.py direct_host.py app_db.py health.py rag.py agent_activity.py; do
    if [[ ! -f "$ROOT/$file" ]]; then
        printf '[FAIL] Required project file is missing: %s\n' "$ROOT/$file" >&2
        exit 1
    fi
done

mkdir -p "$ROOT/shared"
cd "$ROOT"
printf '[INFO] Starting MyForever locally on port 8000.\n'
printf '[INFO] Open http://127.0.0.1:8000/ on this device.\n'
printf '[INFO] Choose a password when prompted. Sign in with username: admin\n'
printf '[INFO] Press Ctrl+C to stop.\n'
exec "$PYTHON" "$ROOT/server.py" "$@"
