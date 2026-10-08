#!/bin/bash
# .claude/hooks/enforce-pipeline.sh
# Runs automatically when the game agent tries to stop.
# Blocks if the game project doesn't meet minimum quality requirements.
# Safety valve: after 5 consecutive blocks, allows stop to prevent infinite loops.
#
# SCOPE: Only enforces on game development agents.
# Bypass: set GCA_SKIP_HOOK=1, or create .claude/hooks/.dev_mode file.

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
ERRORS=""

cfg() {
  python3 "$PROJECT_DIR/.claude/hooks/validation/read-config.py" "$PROJECT_DIR" "$1" "$2"
}

if ! CONFIG_ERROR=$(python3 "$PROJECT_DIR/.claude/hooks/validation/read-config.py" \
  "$PROJECT_DIR" pipeline.min_iterations 1 2>&1 >/dev/null); then
  echo "GCA stop hook blocked: $CONFIG_ERROR" >&2
  exit 2
fi
if ! CONFIG_ERROR=$(python3 "$PROJECT_DIR/.claude/hooks/validation/read-config.py" \
  "$PROJECT_DIR" pipeline.max_iterations 2 2>&1 >/dev/null); then
  echo "GCA stop hook blocked: $CONFIG_ERROR" >&2
  exit 2
fi

MIN_ITERATIONS=$(cfg pipeline.min_iterations 1)
MAX_ITERATIONS=$(cfg pipeline.max_iterations 2)
MAX_CONSECUTIVE_BLOCKS=$(cfg pipeline.max_consecutive_blocks 5)
MIN_SCREENSHOTS=$(cfg pipeline.min_screenshots 3)
MIN_UNIQUE_SCREENSHOTS=$(cfg pipeline.min_unique_screenshots 3)
STALE_TOLERANCE_SECONDS=$(cfg pipeline.staleness_tolerance_seconds 60)
MAX_ANALYSIS_AGE_MINUTES=$(cfg pipeline.max_analysis_age_minutes 30)
if ! [[ "$MIN_ITERATIONS" =~ ^[0-9]+$ ]] \
  || ! [[ "$MAX_ITERATIONS" =~ ^[1-9][0-9]*$ ]] \
  || [ "$MIN_ITERATIONS" -gt "$MAX_ITERATIONS" ]; then
  echo "GCA stop hook blocked: require 0 <= pipeline.min_iterations <= pipeline.max_iterations and max_iterations >= 1." >&2
  exit 2
fi

# ─── Bypass for non-game-agent sessions ──────────────────────────────────
# Skip if explicitly bypassed (framework dev, debugging, etc.)
if [ "${GCA_SKIP_HOOK:-0}" = "1" ]; then
  exit 0
fi
if [ -f "$PROJECT_DIR/.claude/hooks/.dev_mode" ]; then
  exit 0
fi

# Resolve this parent Stop event through the immutable binding created only after
# a successful init_workspace response. Subagent stops and unbound dev sessions
# are out of scope; malformed/pending bindings fail closed.
BINDING_META=$(python3 "$PROJECT_DIR/.claude/hooks/session/guard-agent-boundary.py" \
  --resolve-hook-session)
BINDING_STATUS=$?
if [ "$BINDING_STATUS" -eq 3 ]; then
  exit 0
fi
if [ "$BINDING_STATUS" -ne 0 ]; then
  echo "GCA stop hook blocked: active pipeline session binding is invalid." >&2
  exit 2
fi
GAME_DIR=$(python3 -c '
import json
import sys
value = json.loads(sys.argv[1])
print(value["game_dir"])
' "$BINDING_META" 2>/dev/null)
BINDING_FILE=$(python3 -c '
import json
import sys
value = json.loads(sys.argv[1])
print(value["binding_file"])
' "$BINDING_META" 2>/dev/null)
PIPELINE_BACKGROUND_TASK_COUNT=$(python3 -c '
import json
import sys
value = json.loads(sys.argv[1])
print(value["pipeline_background_task_count"])
' "$BINDING_META" 2>/dev/null)
if [ -z "$GAME_DIR" ] || [ -z "$BINDING_FILE" ] || [ ! -d "$GAME_DIR" ]; then
  echo "GCA stop hook blocked: bound workspace metadata is unavailable." >&2
  exit 2
fi
case "$PIPELINE_BACKGROUND_TASK_COUNT" in
  ''|*[!0-9]*)
    echo "GCA stop hook blocked: background task metadata is unavailable." >&2
    exit 2
    ;;
esac
BLOCK_COUNTER_FILE="$BINDING_FILE.block_count"

# A main-agent Stop with pipeline subagents/workflows still in flight is a
# supported asynchronous pause, not a release attempt. Claude Code will wake the
# parent when those tasks finish. Exclude shell servers, monitors, MCP tasks, and
# unrelated agents in guard-agent-boundary.py so they cannot suppress this gate.
if [ "$PIPELINE_BACKGROUND_TASK_COUNT" -gt 0 ]; then
  rm -f "$BLOCK_COUNTER_FILE"
  exit 0
fi

fail() {
  ERRORS="${ERRORS}  - $1\n"
}

# Safety valve: track consecutive blocks
if [ -f "$BLOCK_COUNTER_FILE" ]; then
  BLOCK_COUNT=$(cat "$BLOCK_COUNTER_FILE")
else
  BLOCK_COUNT=0
fi

block_incomplete_phase() {
  local reason="$1"
  local action="$2"

  BLOCK_COUNT=$((BLOCK_COUNT + 1))
  echo "$BLOCK_COUNT" > "$BLOCK_COUNTER_FILE"

  if [ "$BLOCK_COUNT" -ge "$MAX_CONSECUTIVE_BLOCKS" ]; then
    python3 - "$GAME_DIR" "$GAME_NAME" "$BLOCK_COUNT" "$reason" "$action" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

game_dir = Path(sys.argv[1])
game_name = sys.argv[2]
count = int(sys.argv[3])
reason = sys.argv[4]
action = sys.argv[5]
out = game_dir / ".playtest" / "stop-hook-unresolved.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    json.dumps(
        {
            "schema_version": 1,
            "status": "unresolved",
            "game": game_name,
            "consecutive_blocks": count,
            "issues": reason,
            "next_action": action,
            "recorded_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
        indent=2,
    )
    + "\n"
)
print(
    json.dumps(
        {
            "systemMessage": (
                f"GCA pipeline for {game_name} stopped before verification after "
                f"{count} no-progress Stop attempts. Resume the bound workspace "
                f"and continue with: {action}. Unresolved state: {out}"
            )
        }
    )
)
PY
    rm -f "$BLOCK_COUNTER_FILE"
    exit 0
  fi

  echo "CANNOT STOP ($BLOCK_COUNT/$MAX_CONSECUTIVE_BLOCKS): $GAME_NAME pipeline is incomplete." >&2
  echo "" >&2
  echo "Current progress: $reason" >&2
  echo "ACTION REQUIRED: $action" >&2
  echo "" >&2
  echo "Do not run release checks before the pipeline reaches verification." >&2
  exit 2
}

GAME_NAME=$(basename "$GAME_DIR")
STATE_FILE="$GAME_DIR/.playtest/state.json"

# No-video iterate phase detection (used by several checks below). The fix loop —
# INCLUDING the first pass — runs verify-agent MODE=iterate (no recording pipeline);
# video/performance/playthrough run ONCE in the final MODE=full confirmation. During
# an iterate round the recording + video artifacts are intentionally absent/stale and
# playthrough_recorded is not yet set — do NOT force them (that would defeat the
# deferral). Detect via the newest verify-report's `mode`; the final ship pass writes
# mode:"full" so video/playthrough/performance are still enforced at ship time.
# `verify.skip_video: true` disables the recording pipeline for EVERY pass (including
# the final one), so it puts the whole run in no-video mode.
REPORT_FOR_MODE="$GAME_DIR/.playtest/verify/verify-report.json"
[ -f "$REPORT_FOR_MODE" ] || REPORT_FOR_MODE="$GAME_DIR/.playtest/verify-report.json"
ITERATE_ROUND=false
if [ "$(cfg verify.skip_video false)" = "true" ]; then ITERATE_ROUND=true; fi
if [ -f "$REPORT_FOR_MODE" ]; then
  RMODE=$(python3 - "$REPORT_FOR_MODE" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text())
print((report.get("mode") or "").lower())
PY
)
  if [ "$RMODE" = "iterate" ] || [ "$RMODE" = "full-novideo" ]; then ITERATE_ROUND=true; fi
fi

# A binding exists only after init_workspace succeeds. Missing state at this point
# is an interrupted Startup transaction, not a non-pipeline session.
if [ ! -f "$STATE_FILE" ]; then
  block_incomplete_phase \
    "workspace is bound but .playtest/state.json has not been created" \
    "Run mcp__gca__preflight_check, then persist the Startup bootstrap with mcp__gca__write_pipeline_state."
fi

# Before verify, the final ship checklist is both noisy and misleading: missing
# code/assets/tests are expected producer work. Keep the Stop feedback compact and
# route the orchestrator to the state machine's recorded next action.
PIPELINE_META=$(python3 - "$STATE_FILE" "$GAME_DIR/.playtest/resume.json" <<'PY'
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text())
resume_path = Path(sys.argv[2])
resume = json.loads(resume_path.read_text()) if resume_path.is_file() else {}

def clean(value):
    return str(value or "").replace("\x1f", " ").replace("\n", " ")

print(
    "\x1f".join(
        [
            clean(state.get("phase")),
            clean(state.get("currentStep") or state.get("current_step")),
            clean(state.get("phase_status") or state.get("status")),
            clean(resume.get("next_action") or state.get("next_action")),
        ]
    )
)
PY
) || block_incomplete_phase \
  ".playtest/state.json is unreadable" \
  "Repair or roll forward the state transaction through mcp__gca__write_pipeline_state."

IFS=$'\037' read -r PIPELINE_PHASE PIPELINE_STEP PIPELINE_STATUS PIPELINE_NEXT <<< "$PIPELINE_META"
case "$PIPELINE_PHASE" in
  verify|critic|release|complete|completed)
    ;;
  *)
    [ -n "$PIPELINE_PHASE" ] || PIPELINE_PHASE="unknown"
    [ -n "$PIPELINE_STEP" ] || PIPELINE_STEP="unknown"
    [ -n "$PIPELINE_STATUS" ] || PIPELINE_STATUS="unknown"
    [ -n "$PIPELINE_NEXT" ] || PIPELINE_NEXT="Continue the next game-pipeline phase recorded by the orchestrator."
    block_incomplete_phase \
      "phase=$PIPELINE_PHASE, step=$PIPELINE_STEP, status=$PIPELINE_STATUS" \
      "$PIPELINE_NEXT"
    ;;
esac

# ─── 1. Phaser version ─────────────────────────────────────────────────
if grep -q "phaser@3\|phaser@2" "$GAME_DIR/index.html" 2>/dev/null; then
  fail "Uses Phaser 3/2 — must use Phaser 4"
fi
if ! grep -q "phaser@4" "$GAME_DIR/index.html" 2>/dev/null; then
  fail "index.html missing phaser@4 CDN reference"
fi

# ─── 2. File assets are real; explicit non-file profiles may opt out ───
ASSET_MANIFEST="$GAME_DIR/assets/asset-manifest.json"
if [ ! -f "$ASSET_MANIFEST" ]; then
  fail "Missing assets/asset-manifest.json. Scaffold must create the shared asset contract before gameplay/assets can integrate."
fi

ALLOW_NO_FILE_ASSETS=$(python3 - "$STATE_FILE" "$ASSET_MANIFEST" "$GAME_DIR/.playtest/inputs/layout-contract.json" <<'PY'
import json
import sys
try:
    state = json.load(open(sys.argv[1]))
    manifest = json.load(open(sys.argv[2]))
    layout = json.load(open(sys.argv[3]))
    allowed = {"procedural", "live-text", "composition"}
    file_sources = {"generated-image", "supplied-image", "licensed-image"}
    sources = manifest.get("runtimeSources")
    declared = set()
    file_keys = set()
    invalid = False
    def visit(element):
        nonlocal_invalid = False
        if not isinstance(element, dict):
            return nonlocal_invalid
        source = element.get("sourceStrategy")
        if source in file_sources:
            if element.get("key"):
                file_keys.add(element["key"])
            else:
                nonlocal_invalid = True
        elif source in allowed:
            declared.add(source)
        else:
            nonlocal_invalid = True
        for child in element.get("children") or []:
            nonlocal_invalid = visit(child) or nonlocal_invalid
        return nonlocal_invalid
    for scene in (layout.get("scenes") or {}).values():
        for element in (scene.get("elements") or []):
            invalid = visit(element) or invalid
    ok = (
        state.get("quality_profile") != "art_directed"
        and int(manifest.get("schema_version", 0)) >= 2
        and manifest.get("qualityProfile") == state.get("quality_profile")
        and manifest.get("fileAssetsRequired") is False
        and manifest.get("assets") == []
        and isinstance(sources, list)
        and len(sources) > 0
        and len(set(sources)) == len(sources)
        and set(sources) == declared
        and not file_keys
        and not invalid
        and all(source in allowed and source in declared for source in sources)
    )
    print("true" if ok else "false")
except Exception:
    print("false")
PY
)

ASSET_COUNT=$(find "$GAME_DIR/assets" -type f \( -name "*.png" -o -name "*.jpg" -o -name "*.webp" \) 2>/dev/null | wc -l | tr -d ' ')
if [ "$ASSET_COUNT" -eq 0 ] && [ "$ALLOW_NO_FILE_ASSETS" != "true" ]; then
  fail "No image files in assets/. DO THIS: static assets use mcp__gca__produce_static_asset; sprite actions use produce_validated_sprite_action → rmbg2_video_matting → pack_spritesheet to create per-action *-sheet.png + *-sheet.json."
fi

# ─── 2b. Sprite actions must ship as per-action spritesheets ──
SPRITE_DIR="$GAME_DIR/assets/sprites"
if [ -d "$SPRITE_DIR" ]; then
  SPRITE_CONTRACT_ERRORS=$(python3 - "$SPRITE_DIR" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
pngs = sorted(root.rglob('*-sheet.png'))
jsons = sorted(root.rglob('*-sheet.json'))
missing_json = [p.relative_to(root) for p in pngs if not p.with_suffix('.json').is_file()]
missing_png = [p.relative_to(root) for p in jsons if not p.with_suffix('.png').is_file()]
complete = sum(1 for p in pngs if p.with_suffix('.json').is_file())
legacy = list(root.rglob('sheet-transparent.png')) + list(root.rglob('*-transparent.gif'))
errors = []
if missing_json:
    errors.append('sheet PNG missing same-basename JSON: ' + ', '.join(map(str, missing_json[:5])))
if missing_png:
    errors.append('sheet JSON missing same-basename PNG: ' + ', '.join(map(str, missing_png[:5])))
if legacy and complete == 0:
    errors.append('legacy sprite intermediates exist but no complete per-action sheet pair was found')
print('; '.join(errors))
PY
)
  SPRITE_CONTRACT_STATUS=$?
  if [ "$SPRITE_CONTRACT_STATUS" -ne 0 ]; then
    fail "Could not validate spritesheet sidecars under $SPRITE_DIR."
  elif [ -n "$SPRITE_CONTRACT_ERRORS" ]; then
    fail "$SPRITE_CONTRACT_ERRORS. DO THIS: run rmbg2_video_matting, then mcp__gca__pack_spritesheet for each action. Every <action>-sheet.png must have a same-basename <action>-sheet.json; do not load GIFs or legacy sheet-transparent.png."
  fi
fi

# ─── 2c. Check that sprite prompts/staging use a safe matting background ──
BATCH_FILE="$GAME_DIR/assets/asset_batch.json"
if [ -f "$BATCH_FILE" ]; then
  BAD_PROMPTS=$(python3 - "$BATCH_FILE" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
def has_bg_spec(prompt):
    p = prompt.lower()
    return any(k in p for k in ['ff00ff', 'magenta', '#00f000', 'chroma green', 'solid flat', 'solid background', 'plain background', 'single color background'])
bad = [d['key'] for d in data if 'sprite' in d.get('key','').lower() and not has_bg_spec(d.get('prompt',''))]
if bad: print(', '.join(bad[:5]))
PY
)
  if [ -n "$BAD_PROMPTS" ]; then
    fail "Sprite prompts missing safe solid background spec: $BAD_PROMPTS. DO THIS: regenerate first frames with a safe solid background (#FF00FF magenta or #00F000 chroma green), then run rmbg2_video_matting and pack_spritesheet. postprocess does not auto-detect or remove backgrounds."
  fi
fi

# ─── 3. Code loads assets from files (not just generateTexture) ────────
if { [ -f "$GAME_DIR/src/scenes/BootScene.js" ] || [ -f "$GAME_DIR/src/scenes/PreloadScene.js" ]; } && [ "$ALLOW_NO_FILE_ASSETS" != "true" ]; then
  LOAD_COUNT=$(grep -r "this\.load\.image\|this\.load\.spritesheet\|this\.load\.atlas" "$GAME_DIR/src/" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$LOAD_COUNT" -eq 0 ]; then
    fail "No this.load.image/spritesheet calls in code. DO THIS: Replace generateTexture() code with this.load.spritesheet('key', 'assets/sprites/file.png', {frameWidth, frameHeight}) in preload()."
  fi
fi

# ─── 4. window.__phaserGame exposed ───────────────────────────────────
if ! grep -q "__phaserGame" "$GAME_DIR/src/main.js" 2>/dev/null; then
  fail "main.js missing window.__phaserGame (needed for state-extractor)"
fi

# ─── 5. Tests exist and have reasonable count ──────────────────────────
TEST_COUNT=$(find "$GAME_DIR/.playtest/tests" -type f \( -name "*.js" -o -name "*.mjs" \) 2>/dev/null | wc -l | tr -d ' ')
if [ "$TEST_COUNT" -eq 0 ]; then
  fail "No test files in .playtest/tests/"
fi

# ─── 6. state.json flags ──────────────────────────────────────────────
if [ -f "$STATE_FILE" ]; then
  # Support multiple state.json formats agents might use
  ASSETS_GEN=$(python3 - "$STATE_FILE" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
# Check various possible field names/structures
ag = d.get('assets_generated', False)
if not ag:
  p = d.get('pipeline', {})
  ag = p.get('assets', {}).get('status') == 'done' if isinstance(p, dict) else False
print(ag)
PY
)

  VISUAL_VER=$(python3 - "$STATE_FILE" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
vv = d.get('visual_verified', False)
if not vv:
  p = d.get('pipeline', {})
  vv = p.get('visual_verify', {}).get('status') == 'done' if isinstance(p, dict) else False
print(vv)
PY
)

  PLAYTHROUGH=$(python3 - "$STATE_FILE" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
pr = d.get('playthrough_recorded', False)
if not pr:
  p = d.get('pipeline', {})
  pr = p.get('playthrough', {}).get('status') == 'done' if isinstance(p, dict) else False
print(pr)
PY
)

  FIRST_PASS=$(python3 - "$STATE_FILE" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
fp = d.get('first_pass_complete', False)
if not fp:
  fp = d.get('currentStep') == 'iteration'
if not fp:
  p = d.get('pipeline', {})
  if isinstance(p, dict):
    fp = all(v.get('status') == 'done' for v in p.values() if isinstance(v, dict))
print(fp)
PY
)

  ITERATION=$(python3 - "$STATE_FILE" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
ic = d.get('iteration_count', d.get('iterationCount', 0))
print(ic)
PY
)

  if [ "$ASSETS_GEN" != "True" ]; then
    fail "assets_generated is false in state.json"
  fi
  if [ "$VISUAL_VER" != "True" ]; then
    fail "visual_verified is false — no visual verification done"
  fi
  # playthrough_recorded is set only by the final MODE=full pass (video is deferred to
  # the end). During a no-video iterate round its absence is expected — don't fail; the
  # ready_to_ship gate + the final-pass stop check still enforce it at ship time.
  if [ "$PLAYTHROUGH" != "True" ] && [ "$ITERATE_ROUND" != "true" ]; then
    fail "playthrough_recorded is false — no playthrough recorded"
  fi
  if [ "$FIRST_PASS" != "True" ]; then
    fail "first_pass_complete is false"
  fi
  # Must have done real iterations with verification
  if [ "$FIRST_PASS" = "True" ] && [ "$ITERATION" -lt "$MIN_ITERATIONS" ]; then
    fail "iterationCount=$ITERATION, need >= $MIN_ITERATIONS. Each iteration = verify(MODE=iterate, no-video) → fix ALL blockers → re-verify. MODE=full is reserved for release confirmation. Keep going."
  fi

  # Canonical verification must have run and must exceed iteration count.
  VERIFY_RAN=$(python3 - "$STATE_FILE" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
vr = d.get('verify_runs', 0)
if not vr:
  p = d.get('pipeline', {})
  vr = p.get('verify', {}).get('run_count', 0) if isinstance(p, dict) else 0
print(vr)
PY
)
  if [ "$FIRST_PASS" = "True" ] && [ "${VERIFY_RAN:-0}" -lt 1 ]; then
    fail "verify_runs is 0. Dispatch verify-agent and complete one canonical verification epoch before stopping."
  fi
  if [ "$FIRST_PASS" = "True" ] && [ "${VERIFY_RAN:-0}" -le "$ITERATION" ]; then
    fail "verify_runs($VERIFY_RAN) <= iteration_count($ITERATION). Every fix MUST be followed by a re-verify."
  fi
fi

# ─── 6b. Deep state validation (audit log, timestamps, screenshots) ───
STATE_VALIDATION=$(python3 "$PROJECT_DIR/.claude/hooks/validation/validate-state-json.py" "$GAME_DIR" 2>&1)
if [ $? -ne 0 ]; then
  FORMATTED_STATE_ERRORS=$(printf '%s\n' "$STATE_VALIDATION" | grep "^  -" || true)
  if [ -n "$FORMATTED_STATE_ERRORS" ]; then
    while IFS= read -r line; do
      fail "${line#  - }"
    done <<< "$FORMATTED_STATE_ERRORS"
  else
    fail "$STATE_VALIDATION"
  fi
fi


# ─── 7. Video recording and analysis (conditional) ──────────────────────
# Video/recording is strongly recommended but not a hard gate when other
# evidence (play-report + screenshots) is available. Only fail when ALL
# gameplay evidence is missing.
# Current layout puts all verify artifacts under .playtest/verify/ (with
# legacy fallbacks for partially-migrated games).
RECORDING_COUNT=$(find "$GAME_DIR/.playtest/verify/recording" "$GAME_DIR/.playtest/verify/recordings" "$GAME_DIR/.playtest/recordings" -type f \( -name "*.webm" -o -name "*.mp4" \) 2>/dev/null | wc -l | tr -d ' ')
# Per-scenario analyses live at verify/recording/<scenario>/video-analysis.json.
# Pick the NEWEST one (by mtime) for the freshness check; fall back to legacy paths.
VIDEO_ANALYSIS=$(find "$GAME_DIR/.playtest/verify/recording" "$GAME_DIR/.playtest/verify/video" "$GAME_DIR/.playtest/video" -name video-analysis.json 2>/dev/null -print0 | xargs -0 ls -t 2>/dev/null | head -1)
[ -n "$VIDEO_ANALYSIS" ] || VIDEO_ANALYSIS="$GAME_DIR/.playtest/video-analysis.json"
PLAY_REPORT="$GAME_DIR/.playtest/verify/play/agent-play-report.json"
[ -f "$PLAY_REPORT" ] || PLAY_REPORT="$GAME_DIR/.playtest/play/agent-play-report.json"
SCREENSHOT_STATS=$(python3 - "$GAME_DIR" <<'PY'
import hashlib
import sys
from pathlib import Path

game_dir = Path(sys.argv[1])
roots = [
    game_dir / ".playtest" / "verify" / "test-run" / "screenshots",
    game_dir / ".playtest" / "verify" / "play" / "frames",
]
screenshots = sorted(
    {
        path
        for root in roots
        if root.is_dir()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".png"
    }
)
hashes = {
    hashlib.sha256(path.read_bytes()).hexdigest()
    for path in screenshots
}
print(f"{len(screenshots)} {len(hashes)}")
PY
)
read -r SCREENSHOT_COUNT UNIQUE_SCREENSHOTS <<< "$SCREENSHOT_STATS"
[ -n "$SCREENSHOT_COUNT" ] || SCREENSHOT_COUNT=0
[ -n "$UNIQUE_SCREENSHOTS" ] || UNIQUE_SCREENSHOTS=0

HAS_PLAY_REPORT=false
if [ -f "$PLAY_REPORT" ]; then
  HAS_PLAY_REPORT=true
fi

# Hard fail only when ALL gameplay evidence is missing
if [ "$RECORDING_COUNT" -eq 0 ] && [ "$HAS_PLAY_REPORT" = "false" ] && [ "$SCREENSHOT_COUNT" -lt "$MIN_SCREENSHOTS" ]; then
  fail "No gameplay evidence: no recordings, no play-report, insufficient screenshots. Need at least one of: video recording, agent-play-report.json, or $MIN_SCREENSHOTS screenshots."
fi

# If video-analysis.json exists, enforce freshness
if [ -f "$VIDEO_ANALYSIS" ]; then
  ANALYSIS_STALE=$(python3 - "$GAME_DIR" "$VIDEO_ANALYSIS" "$STALE_TOLERANCE_SECONDS" <<'PY' 2>/dev/null
import os
import sys
from pathlib import Path

game_dir = Path(sys.argv[1])
va_mtime = os.path.getmtime(sys.argv[2])
stale_tolerance = float(sys.argv[3])
src_dir = game_dir / 'src'
if src_dir.exists():
    latest = max((f.stat().st_mtime for f in src_dir.rglob('*.js')), default=0)
    print('stale' if latest > va_mtime + stale_tolerance else 'ok')
else:
    print('ok')
PY
)
  # Skip video-staleness enforcement during a no-video iterate round — video is
  # deferred to the final MODE=full pass (which refreshes it and flips mode→full).
  if [ "$ANALYSIS_STALE" = "stale" ] && [ "$ITERATE_ROUND" != "true" ]; then
    fail "video-analysis.json is older than latest code changes. Dispatch verify-agent for a canonical MODE=full run_verify_dag pass."
  fi

  # video-analysis.json must be recent (not stale from an old run) — again, not during
  # a no-video iterate round where a stale earlier-pass analysis is expected.
  if [ "$ITERATION" -gt 0 ] && [ "$ITERATE_ROUND" != "true" ]; then
    ANALYSIS_AGE=$(python3 - "$VIDEO_ANALYSIS" <<'PY' 2>/dev/null
import os
import sys
import time

mtime = os.path.getmtime(sys.argv[1])
age_min = (time.time() - mtime) / 60
print(int(age_min))
PY
)
    if [ "${ANALYSIS_AGE:-999}" -gt "$MAX_ANALYSIS_AGE_MINUTES" ]; then
      fail "video-analysis.json is ${ANALYSIS_AGE}min old. Dispatch verify-agent for a fresh canonical MODE=full run_verify_dag pass."
    fi
  fi
fi

# verify_runs must be > iteration_count (each fix requires a re-verify)
if [ "$FIRST_PASS" = "True" ] && [ "${VERIFY_RAN:-0}" -le "$ITERATION" ]; then
  fail "verify_runs($VERIFY_RAN) <= iteration_count($ITERATION). Every fix MUST be followed by a canonical re-verification through verify-agent."
fi

# Cross-validate: screenshots must actually exist
if [ "$FIRST_PASS" = "True" ] && [ "$SCREENSHOT_COUNT" -lt "$MIN_SCREENSHOTS" ]; then
  fail "Only $SCREENSHOT_COUNT canonical gameplay screenshots found. Need at least $MIN_SCREENSHOTS (title, gameplay, gameover). Dispatch verify-agent; run_verify_dag owns the unfiltered tests/play capture and aggregate report."
fi

VERIFY_REPORT="$GAME_DIR/.playtest/verify/verify-report.json"
[ -f "$VERIFY_REPORT" ] || VERIFY_REPORT="$GAME_DIR/.playtest/verify-report.json"
if [ ! -f "$VERIFY_REPORT" ]; then
  fail "No verify-report.json found. Dispatch verify-agent for canonical run_verify_dag; the DAG owns producer fan-in and report finalization."
else
  REPORT_STALE=$(python3 - "$GAME_DIR" "$VERIFY_REPORT" "$STALE_TOLERANCE_SECONDS" <<'PY' 2>/dev/null
import os
import sys
from pathlib import Path

game_dir = Path(sys.argv[1])
report_mtime = os.path.getmtime(sys.argv[2])
stale_tolerance = float(sys.argv[3])
src_dir = game_dir / 'src'
if src_dir.exists():
    latest = max((f.stat().st_mtime for f in src_dir.rglob('*.js')), default=0)
    print('stale' if latest > report_mtime + stale_tolerance else 'ok')
else:
    print('ok')
PY
)
  if [ "$REPORT_STALE" = "stale" ]; then
    fail "verify-report.json is older than latest code changes. Dispatch verify-agent for a fresh canonical run_verify_dag pass."
  fi
fi

# ─── 7b. Authoritative release composite must have passed ──────────────
# The checks above provide actionable diagnostics, but they are not a second
# release authority. Only the identity-bound result emitted by the MCP
# `ready_to_ship` composite can authorize a clean stop.
READY_GATE="$GAME_DIR/.playtest/verify/gate-results/ready_to_ship.json"
ACTUAL_BUILD_DIGEST=$(PYTHONPATH="$PROJECT_DIR/gca-mcp/src" python3 - "$GAME_DIR" "$PROJECT_DIR/gca.config.yaml" <<'PY'
import sys
from pathlib import Path
from gca_mcp.evidence import compute_build_digest
try:
    print(compute_build_digest(Path(sys.argv[1]), Path(sys.argv[2])))
except Exception:
    pass
PY
)
if [ -z "$ACTUAL_BUILD_DIGEST" ]; then
  fail "Could not compute the canonical build digest; release evidence cannot be trusted."
fi
if [ ! -f "$READY_GATE" ]; then
  fail "No current ready_to_ship gate result. Run mcp__gca__gate_check({gate:'ready_to_ship'}) after the final MODE=full verification and critic."
else
  READY_STATUS=$(python3 - "$STATE_FILE" "$GAME_DIR/.playtest/verify/current-run.json" "$READY_GATE" "$ACTUAL_BUILD_DIGEST" <<'PY'
import json
import sys
from pathlib import Path

try:
    state = json.loads(Path(sys.argv[1]).read_text())
    run = json.loads(Path(sys.argv[2]).read_text())
    gate = json.loads(Path(sys.argv[3]).read_text())
    actual_digest = sys.argv[4]
    same = all(
        gate.get(key) == run.get(key)
        for key in ("pipeline_run_id", "build_digest", "verify_run_id")
    )
    same = same and all(
        run.get(key) == state.get(key)
        for key in (
            "pipeline_run_id",
            "build_digest",
            "quality_profile",
            "level_model",
        )
    )
    mode_ok = run.get("mode") in ("full", "full-novideo")
    passed = gate.get("gate") == "ready_to_ship" and gate.get("blocked") is False
    digest_ok = bool(actual_digest) and state.get("build_digest") == actual_digest
    print("ok" if same and mode_ok and passed and digest_ok else "invalid")
except Exception:
    print("invalid")
PY
)
  if [ "$READY_STATUS" != "ok" ]; then
    fail "ready_to_ship result is blocked, stale, cross-run, or not from a final full verification epoch."
  fi
fi

ASSET_INTEGRATION_REPORT="$GAME_DIR/.playtest/verify/assets/asset-integration-report.json"
[ -f "$ASSET_INTEGRATION_REPORT" ] || ASSET_INTEGRATION_REPORT="$GAME_DIR/.playtest/assets/asset-integration-report.json"
[ -f "$ASSET_INTEGRATION_REPORT" ] || ASSET_INTEGRATION_REPORT="$GAME_DIR/.playtest/asset-integration-report.json"
if [ ! -f "$ASSET_INTEGRATION_REPORT" ]; then
  fail "No asset-integration-report.json found. DO THIS: run asset-integration-agent after Implement and Assets join."
else
  ASSET_BLOCKERS=$(python3 - "$ASSET_INTEGRATION_REPORT" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
print(len(d.get('blocking_issues', [])))
PY
)
  if [ "${ASSET_BLOCKERS:-0}" -gt 0 ]; then
    fail "asset-integration-report.json has ${ASSET_BLOCKERS} blocking issue(s). Route them before verification."
  fi
fi

# ─── 8. Canonical gameplay screenshots must exist and be diverse ─────
if [ "$SCREENSHOT_COUNT" -lt "$MIN_SCREENSHOTS" ]; then
  fail "Only $SCREENSHOT_COUNT screenshots. Need at least $MIN_SCREENSHOTS (title, gameplay, gameover)."
fi

# Check screenshot diversity (not all identical)
if [ "$SCREENSHOT_COUNT" -ge "$MIN_SCREENSHOTS" ]; then
  if [ "$UNIQUE_SCREENSHOTS" -lt "$MIN_UNIQUE_SCREENSHOTS" ]; then
    fail "Only $UNIQUE_SCREENSHOTS unique screenshots out of $SCREENSHOT_COUNT. Agent is repeating the same screenshot without fixing issues."
  fi
fi

# ─── 9. Critic agent must have reviewed and not REJECTED ─────────────
CRITIC_REPORT="$GAME_DIR/.playtest/verify/critic/critic-report.json"
[ -f "$CRITIC_REPORT" ] || CRITIC_REPORT="$GAME_DIR/.playtest/critic/critic-report.json"
[ -f "$CRITIC_REPORT" ] || CRITIC_REPORT="$GAME_DIR/.playtest/critic-report.json"
if [ ! -f "$CRITIC_REPORT" ]; then
  fail "No critic-report.json. MUST dispatch critic agent (see references/critic-agent.md) after every iteration. The critic is an independent reviewer — you cannot skip it."
else
  CRITIC_VERDICT=$(python3 - "$GAME_DIR" "$CRITIC_REPORT" "$STALE_TOLERANCE_SECONDS" <<'PY' 2>/dev/null
import json
import os
import sys
from pathlib import Path

game_dir = Path(sys.argv[1])
critic_report = Path(sys.argv[2])
stale_tolerance = float(sys.argv[3])
d = json.loads(critic_report.read_text())
verdict = d.get('verdict', 'UNKNOWN')
# Check freshness — must be newer than latest src/ change
src_dir = game_dir / 'src'
if src_dir.exists():
    latest = max((f.stat().st_mtime for f in src_dir.rglob('*.js')), default=0)
    report_mtime = os.path.getmtime(critic_report)
    if latest > report_mtime + stale_tolerance:
        verdict = 'STALE'
print(verdict)
PY
)

  if [ "$CRITIC_VERDICT" = "REJECT" ]; then
    BLOCKING=$(python3 - "$CRITIC_REPORT" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text())
issues = d.get('blocking_issues', [])
for i in issues[:3]:
    print(i)
PY
)
    fail "Critic agent verdict: REJECT. Blocking issues: $BLOCKING"
  elif [ "$CRITIC_VERDICT" = "STALE" ]; then
    fail "critic-report.json is stale (older than latest code changes). Re-dispatch critic agent."
  elif [ "$CRITIC_VERDICT" = "UNKNOWN" ]; then
    fail "critic-report.json has no valid verdict field. Re-dispatch critic agent."
  fi
fi

# ─── Output ───────────────────────────────────────────────────────────
if [ -n "$ERRORS" ]; then
  # The outer fix-loop budget is authoritative. Once consumed, preserve the
  # blockers as unresolved evidence and allow this run to stop; never grind on
  # until the unrelated consecutive-Stop safety valve happens to open.
  if [[ "${ITERATION:-}" =~ ^[0-9]+$ ]] \
    && [ "$ITERATION" -ge "$MAX_ITERATIONS" ]; then
    GCA_HOOK_ERRORS="$ERRORS" python3 - \
      "$GAME_DIR" "$GAME_NAME" "$ITERATION" "$MAX_ITERATIONS" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

game_dir = Path(sys.argv[1])
game_name = sys.argv[2]
iteration_count = int(sys.argv[3])
max_iterations = int(sys.argv[4])
issues = os.environ.get("GCA_HOOK_ERRORS", "").replace("\\n", "\n").strip()
state_path = game_dir / ".playtest" / "state.json"
try:
    state = json.loads(state_path.read_text())
except (OSError, json.JSONDecodeError):
    state = {}
report = {
    "schema_version": 1,
    "status": "unresolved",
    "reason": "iteration_budget_exhausted",
    "game": game_name,
    "pipeline_run_id": state.get("pipeline_run_id"),
    "build_digest": state.get("build_digest"),
    "iteration_count": iteration_count,
    "max_iterations": max_iterations,
    "issues": issues,
    "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
out = game_dir / ".playtest" / "stop-hook-unresolved.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({
    "systemMessage": (
        f"GCA pipeline for {game_name} exhausted its automatic iteration "
        f"budget ({iteration_count}/{max_iterations}). Remaining blockers were "
        f"written to {out}; this is unresolved, not shipped. Ask whether to "
        "continue with a larger budget or abandon the run."
    )
}))
PY
    rm -f "$BLOCK_COUNTER_FILE"
    exit 0
  fi

  # Increment block counter
  BLOCK_COUNT=$((BLOCK_COUNT + 1))
  echo "$BLOCK_COUNT" > "$BLOCK_COUNTER_FILE"

  # Safety escape: end the session after bounded retries, but persist/report the
  # unresolved state. This is not a gate pass and never rewrites evidence.
  if [ "$BLOCK_COUNT" -ge "$MAX_CONSECUTIVE_BLOCKS" ]; then
    GCA_HOOK_ERRORS="$ERRORS" python3 - "$GAME_DIR" "$GAME_NAME" "$BLOCK_COUNT" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

game_dir = Path(sys.argv[1])
game_name = sys.argv[2]
count = int(sys.argv[3])
issues = os.environ.get("GCA_HOOK_ERRORS", "").replace("\\n", "\n").strip()
report = {
    "schema_version": 1,
    "status": "unresolved",
    "game": game_name,
    "consecutive_blocks": count,
    "issues": issues,
    "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
out = game_dir / ".playtest" / "stop-hook-unresolved.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({
    "systemMessage": (
        f"GCA pipeline for {game_name} remains BLOCKED after {count} Stop-hook "
        f"retries. Unresolved evidence was written to {out}; this was not a "
        "successful gate result. Start/resume the bound game session to continue."
    )
}))
PY
    rm -f "$BLOCK_COUNTER_FILE"
    exit 0
  fi

  echo "CANNOT STOP ($BLOCK_COUNT/$MAX_CONSECUTIVE_BLOCKS): $GAME_NAME does not meet ship requirements." >&2
  echo "" >&2
  echo "Issues:" >&2
  echo -e "$ERRORS" >&2
  echo "" >&2
  echo "ACTION REQUIRED: Continue the game-pipeline verification loop until ship-quality:" >&2
  echo "  Dispatch verify-agent, fix all blocking issues by owner, rerun critic, then run mcp__gca__gate_check({gate:'ready_to_ship'})." >&2
  echo "" >&2
  echo "Do NOT stop or mark the pipeline complete without a current ready_to_ship PASS." >&2
  exit 2
fi

# All checks pass — reset counter and allow stop
rm -f "$BLOCK_COUNTER_FILE"
exit 0
