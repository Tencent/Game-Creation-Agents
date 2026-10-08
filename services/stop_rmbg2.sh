#!/usr/bin/env bash
# Stop the RMBG-2.0 service and any watchdog guarding it.
#   bash stop_rmbg2.sh
set -uo pipefail

echo "[stop] stopping watchdog (if any)"
pkill -f "watchdog_rmbg2.sh" 2>/dev/null || true

echo "[stop] stopping rmbg2_server"
pkill -f "rmbg2_server.py" 2>/dev/null || true
sleep 3

if pgrep -f "rmbg2_server.py" >/dev/null 2>&1; then
  echo "[stop] still alive — sending SIGKILL"
  pkill -9 -f "rmbg2_server.py" 2>/dev/null || true
fi

echo "[stop] done"
