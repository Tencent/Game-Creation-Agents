#!/usr/bin/env bash
# Watchdog for the RMBG-2.0 service — keeps it alive without systemd.
#
# Polls /health every CHECK_INTERVAL seconds; if the service is down or
# unresponsive, it calls start_rmbg2.sh to bring it back. A flock guard
# ensures only one watchdog runs at a time (re-running this script is a no-op
# if a watchdog is already active).
#
# Launch it detached so it survives your shell:
#   setsid bash watchdog_rmbg2.sh >> /tmp/rmbg2_logs/watchdog.log 2>&1 < /dev/null &
#
# Stop it:  pkill -f watchdog_rmbg2.sh   (or bash stop_rmbg2.sh)
set -uo pipefail

SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${RMBG_PORT:-8080}"
CHECK_INTERVAL="${RMBG_WATCHDOG_INTERVAL:-30}"
LOG_DIR="/tmp/rmbg2_logs"
WD_LOCK="${LOG_DIR}/watchdog.lock"

mkdir -p "$LOG_DIR"

# Single-instance guard: if another watchdog holds the lock, exit quietly.
exec 8>"$WD_LOCK"
if ! flock -n 8; then
  echo "[watchdog] another watchdog already running — exiting"
  exit 0
fi

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[watchdog $(ts)] started — polling :${PORT}/health every ${CHECK_INTERVAL}s"

while true; do
  if curl -s -m 5 "http://localhost:${PORT}/health" 2>/dev/null | grep -q '"status":"ok"'; then
    :  # healthy
  else
    echo "[watchdog $(ts)] service DOWN — restarting"
    bash "${SERVICE_DIR}/start_rmbg2.sh" || echo "[watchdog $(ts)] restart attempt failed"
  fi
  sleep "$CHECK_INTERVAL"
done
