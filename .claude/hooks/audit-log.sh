#!/bin/bash
# Append an event to the game's audit log.
# Usage: audit-log.sh <game_dir> <event_type> [details...]
#
# Guarded events (counted against state.json claims): verify_run, iteration_complete, assets_generated
# Other events: pipeline_start
# The hook and pipeline validate the guarded entries against state.json claims.

GAME_DIR="$1"
EVENT="$2"
shift 2
DETAILS="$*"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

if [ -z "$GAME_DIR" ] || [ -z "$EVENT" ]; then
  echo "Usage: audit-log.sh <game_dir> <event_type> [details]" >&2
  exit 1
fi
if ! MAX_ITERATIONS=$(python3 \
  "$PROJECT_DIR/.claude/hooks/validation/read-config.py" \
  "$PROJECT_DIR" pipeline.max_iterations 2); then
  echo "[audit-log] Cannot read pipeline.max_iterations." >&2
  exit 2
fi
if ! [[ "$MAX_ITERATIONS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[audit-log] pipeline.max_iterations must be a positive integer." >&2
  exit 2
fi

AUDIT_FILE="$GAME_DIR/.playtest/audit.jsonl"
mkdir -p "$(dirname "$AUDIT_FILE")"

TIMESTAMP=$(date +%s)
ISO_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date +"%Y-%m-%dT%H:%M:%SZ")

# Write one identity-bound JSONL entry. Arguments, rather than interpolated
# Python source, keep arbitrary details from corrupting/injecting the record.
python3 - "$GAME_DIR" "$EVENT" "$DETAILS" "$TIMESTAMP" "$ISO_TIME" \
  "$MAX_ITERATIONS" <<'PY' >> "$AUDIT_FILE"
import json
import sys
from pathlib import Path

game_dir = Path(sys.argv[1])
entry = {
    "timestamp": int(sys.argv[4]),
    "time_iso": sys.argv[5],
    "event": sys.argv[2],
    "details": sys.argv[3],
}
state_path = game_dir / ".playtest" / "state.json"
try:
    state = json.loads(state_path.read_text())
except Exception:
    state = {}
if entry["event"] == "iteration_complete":
    iteration_count = state.get("iteration_count", state.get("iterationCount", 0))
    max_iterations = int(sys.argv[6])
    if (
        isinstance(iteration_count, bool)
        or not isinstance(iteration_count, int)
        or iteration_count < 0
    ):
        print(
            "[audit-log] Cannot append iteration_complete: state iteration_count "
            "is invalid.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if iteration_count >= max_iterations:
        print(
            "[audit-log] ITERATION_BUDGET_EXHAUSTED: iteration_count="
            f"{iteration_count}, pipeline.max_iterations={max_iterations}. "
            "Report unresolved blockers instead of starting another iteration.",
            file=sys.stderr,
        )
        raise SystemExit(2)
# Stamp identity both-or-neither, matching read_pipeline_identity (evidence.py):
# a half-stamped row (run_id without digest) is never produced by the Python
# writer, and the counter drops rows whose pipeline_run_id does not match.
run_id = state.get("pipeline_run_id")
digest = state.get("build_digest")
if isinstance(run_id, str) and run_id and isinstance(digest, str) and digest:
    entry["pipeline_run_id"] = run_id
    entry["build_digest"] = digest
elif entry["event"] in ("verify_run", "iteration_complete", "assets_generated"):
    # Guarded event that could not be identity-stamped: the counter will ignore
    # it, silently undercounting. Surface it instead of dropping it quietly.
    print(
        f"[audit-log] WARNING: {entry['event']} written without pipeline_run_id "
        f"(state.json unreadable?); it will be ignored by the counter.",
        file=sys.stderr,
    )
print(json.dumps(entry, ensure_ascii=False))
PY
