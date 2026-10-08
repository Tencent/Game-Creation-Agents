#!/usr/bin/env bash
# Idempotent launcher for the multi-GPU RMBG-2.0 service.
# Safe to call repeatedly — if the service is already healthy it does nothing.
#
#   bash start_rmbg2.sh          # start if not running
#   bash start_rmbg2.sh --force  # restart even if running
#
# Detaches via setsid so the server survives the launching shell exiting.
set -euo pipefail

# Python interpreter: override with RMBG_PYTHON to point at your project env.
PY="${RMBG_PYTHON:-python}"
SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${RMBG_PORT:-8080}"
LOG_DIR="/tmp/rmbg2_logs"
LOG="${LOG_DIR}/server.log"
LOCK="${LOG_DIR}/start.lock"

mkdir -p "$LOG_DIR"

is_healthy() {
  curl -s -m 5 "http://localhost:${PORT}/health" 2>/dev/null | grep -q '"status":"ok"'
}

# Serialize concurrent launches (watchdog + manual) so we never double-start.
exec 9>"$LOCK"
flock 9

if [[ "${1:-}" == "--force" ]]; then
  echo "[start] --force: stopping any running instance first"
  pkill -f "rmbg2_server.py" 2>/dev/null || true
  sleep 4
fi

if is_healthy; then
  echo "[start] already healthy on :${PORT} — nothing to do"
  exit 0
fi

# Stale process but not answering? clean it up.
if pgrep -f "rmbg2_server.py" >/dev/null 2>&1; then
  echo "[start] found a non-healthy rmbg2_server process — killing it"
  pkill -f "rmbg2_server.py" 2>/dev/null || true
  sleep 4
fi

echo "[start] launching RMBG-2.0 server (log: ${LOG})"
cd "$SERVICE_DIR"
setsid "$PY" rmbg2_server.py >>"$LOG" 2>&1 </dev/null &
disown || true

# Wait for model load across all GPUs (an 8-card cold-start can take ~60-90s;
# fewer cards is faster). The 120s ceiling covers the largest configs.
for i in $(seq 1 120); do
  if is_healthy; then
    echo "[start] healthy on :${PORT} after ${i}s"
    exit 0
  fi
  sleep 1
done

echo "[start] WARN: not healthy within 120s — process may still be loading; see ${LOG}" >&2
exit 1
