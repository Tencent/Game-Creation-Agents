/**
 * playtest-layout.mjs — Canonical `.playtest/` layout for the bundled Node
 * verification scripts. Mirror of the Python `playtest_layout.py` so producers
 * (test runner, recorder, video analyzer) and consumers
 * (gate-check) agree on where every artifact lives.
 *
 * Layout: grouped by pipeline stage, each report co-located with its evidence.
 * See playtest_layout.py for the full tree + rationale.
 */

import { existsSync } from 'fs';
import { join } from 'path';

// ── Top-level groups (relative to <game>/.playtest) ───────────────────────
export const TESTS_DIR = 'tests';        // test SOURCES (inputs)
export const INPUTS_DIR = 'inputs';      // authored inputs / intermediates
export const PREFLIGHT_DIR = 'preflight'; // pre-pipeline checks (env + asset smoke)
export const VERIFY_DIR = 'verify';       // umbrella for ALL verification outputs
// Dedicated home for validation INTERMEDIATES — a SEPARATE top-level dir from
// verify/ so this flow never overlaps the canonical verification outputs.
export const VERIFY_MIDDLE_DIR = 'verify_middle';

// ── Authored inputs / intermediates (under inputs/) ───────────────────────
export const RUBRIC = `${INPUTS_DIR}/rubric.json`;
// Interaction contract (scaffold-authored from GDD §0.2) — the unified SSOT for
// player input (`actions`) AND collision topology (`layers`/`matrix`/`world`/
// `character`/`entities`). Renamed + extended from the old `control-map.json`
// (`pick` is both a pointer input and a hit test, so input and collision were
// always one contract). CONTROL_MAP stays as a transition alias so play-agent
// (which only reads `.actions`) keeps working with zero changes.
export const INTERACTION_CONTRACT = `${INPUTS_DIR}/interaction-contract.json`;
export const CONTROL_MAP = INTERACTION_CONTRACT;   // transition alias — remove once all refs use the new name
export const OBJECTIVES = `${INPUTS_DIR}/objectives.json`;
// Scene layout contract (scaffold-authored from GDD §2.0.2) — the lays-out-like
// SSOT for element POSITION (per scene: key + box[x,y,w,h] (0..1) + origin +
// anchorTo + kind). gateLayoutAlignment compares it against the rendered bounds
// dump (LAYOUT_RENDERED_DIR).
export const LAYOUT_CONTRACT = `${INPUTS_DIR}/layout-contract.json`;
// One interaction script per scenario (early-game / boss-fight / near-death / …).
export const INTERACTIONS_DIR = `${INPUTS_DIR}/interactions`;  // <scenario>.mjs
export const SCENARIOS_DIR = `${INPUTS_DIR}/scenarios`;        // optional <scenario>.json seeds
export const INTERACT_SCRIPT = `${INPUTS_DIR}/interact.mjs`;   // legacy single-script
export const DISPLAY_PARAMS = `${INPUTS_DIR}/display-params.json`;

// ── Verification sub-directories (under verify/) ──────────────────────────
export const GATE_RESULTS_DIR = `${VERIFY_DIR}/gate-results`;
export const VERIFY_CHECKPOINTS_DIR = `${VERIFY_DIR}/checkpoints`;
// Recordings are PER-SCENARIO: verify/recording/<scenario>/ holds that scenario's
// capture (recording.webm + state/perf) and its analysis (video-analysis.json + evidence/).
export const RECORDING_DIR = `${VERIFY_DIR}/recording`;
export const RECORDINGS_DIR = RECORDING_DIR; // back-compat alias
export const VIDEO_DIR = RECORDING_DIR;      // back-compat alias
export const RUNTIME_DIR = `${VERIFY_DIR}/runtime`;
export const PLAY_DIR = `${VERIFY_DIR}/play`;
export const TEST_RUN_DIR = `${VERIFY_DIR}/test-run`;
export const ALIGNMENT_DIR = `${VERIFY_DIR}/alignment`;
export const CRITIC_DIR = `${VERIFY_DIR}/critic`;
export const ASSETS_DIR = `${VERIFY_DIR}/assets`;

// ── Canonical artifact paths (relative to <game>/.playtest) ───────────────
export const VERIFY_REPORT = `${VERIFY_DIR}/verify-report.json`;
export const VERIFY_RUN = `${VERIFY_DIR}/current-run.json`;
export const RUNTIME_TOKEN_RECEIPT = `${RUNTIME_DIR}/runtime-token.json`;
export const RUNTIME_ASSETS_RECEIPT = `${RUNTIME_DIR}/runtime-assets.json`;
export const VIDEO_RECEIPT_NAME = 'video-receipt.json';
export const ASSET_SMOKE_DIR = PREFLIGHT_DIR;
export const PREFLIGHT_REPORT = `${PREFLIGHT_DIR}/preflight-report.json`;
export const ASSET_SMOKE_REPORT = `${PREFLIGHT_DIR}/asset-smoke-report.json`;
export const TEST_RESULTS = `${TEST_RUN_DIR}/test-results.json`;
export const TEST_SCREENSHOTS_DIR = `${TEST_RUN_DIR}/screenshots`;
export const TEST_RECORDINGS_DIR = `${TEST_RUN_DIR}/recordings`;
export const VIDEO_ANALYSIS = `${VIDEO_DIR}/video-analysis.json`;
export const VIDEO_EVIDENCE_SUBDIR = 'evidence'; // under the analysis output's dir
export const PLAY_REPORT = `${PLAY_DIR}/agent-play-report.json`;
export const PLAY_HISTORY = `${PLAY_DIR}/agent-play-history.jsonl`;
export const VISUAL_ALIGNMENT = `${ALIGNMENT_DIR}/visual/alignment.json`;
export const GAMEPLAY_ALIGNMENT = `${ALIGNMENT_DIR}/gameplay/alignment.json`;
// Per-element alignment axis (Axis 3): each §2E UI element's running render vs
// its concept crop (looks-like, element granularity). Sibling of visual/gameplay.
export const ELEMENT_ALIGNMENT = `${ALIGNMENT_DIR}/element/alignment.json`;
// Physics alignment axis (Axis 4): deterministic interaction body/art/world
// checks projected into the verify epoch. Sibling of visual/gameplay/element.
export const PHYSICS_ALIGNMENT = `${ALIGNMENT_DIR}/physics/alignment.json`;
// Layout alignment axis (Axis 5): the running game's measured element positions
// (LAYOUT_RENDERED_DIR) vs the §2.0.2 layout-contract.json. WRITTEN by
// materialize_layout_alignment; gateLayoutAlignment later consumes it for the
// blocking decision. Sibling of the other alignment axes.
export const LAYOUT_ALIGNMENT = `${ALIGNMENT_DIR}/layout/alignment.json`;
export const CRITIC_REPORT = `${CRITIC_DIR}/critic-report.json`;
export const ASSET_INTEGRATION_REPORT = `${ASSETS_DIR}/asset-integration-report.json`;
// Per-entity slot-fit verdicts (assets-agent → one <key>.json each). Under
// verify_middle/ so this validation intermediate stays out of the verify/ tree.
export const SLOT_VALIDATION_DIR = `${VERIFY_MIDDLE_DIR}/slot`;
// (a) Entity-scale audit written by gateEntityScale (per-entity rendered px +
// verdicts) — same verify_middle/ home as slot/.
export const SCALE_VALIDATION_DIR = `${VERIFY_MIDDLE_DIR}/scale`;
export const SCALE_REPORT = `${SCALE_VALIDATION_DIR}/scale-report.json`;
// UI proportion audit written by gateUiProportion (per-element displayed px,
// canvas fraction, texture-downscale + measured-proportion verdicts).
export const UI_SCALE_VALIDATION_DIR = `${VERIFY_MIDDLE_DIR}/ui-scale`;
export const UI_SCALE_REPORT = `${UI_SCALE_VALIDATION_DIR}/ui-scale-report.json`;
// (Axis 5) Per-scene rendered element bounds dumped by the Playwright bounds
// capture (named-object getBounds per scene → <scene>.rendered.json). The
// MEASURED side of the layout axis; materialize_layout_alignment reads it +
// LAYOUT_CONTRACT and writes LAYOUT_ALIGNMENT. Same verify_middle/ home as
// scale/ and ui-scale/.
export const LAYOUT_RENDERED_DIR = `${VERIFY_MIDDLE_DIR}/layout`;
// Comprehensive VISUAL verification (element-visual-diff.mjs): per-scene running
// screenshots + per-element rendered crops, and the deterministic sidecar that
// gateVisualDiff requires (proves the global+element image-diff loop actually ran).
// Same verify_middle/ home as layout/ and ui-scale/.
export const VISUAL_DIFF_DIR = `${VERIFY_MIDDLE_DIR}/visual`;
export const VISUAL_DIFF_REPORT = `${VISUAL_DIFF_DIR}/visual-diff-report.json`;
export const VISUAL_DIFF_HISTORY = `${VISUAL_DIFF_DIR}/visual-diff-history.jsonl`;

// ── Legacy → backward-compatible reads during/after migration ─────────────
// [per-stage layout, original flat layout]
const LEGACY = {
  [RUBRIC]: ['rubric.json'],
  // New name first; fall back to the old control-map.json (inputs/, then the
  // pre-migration play/ locations) so existing games still resolve.
  [INTERACTION_CONTRACT]: ['inputs/control-map.json', 'verify/play/control-map.json', 'play/control-map.json'],
  [OBJECTIVES]: ['verify/play/objectives.json', 'play/objectives.json'],
  [INTERACT_SCRIPT]: ['interact.mjs'],
  [DISPLAY_PARAMS]: ['display-params.json'],
  [VERIFY_REPORT]: ['verify-report.json'],
  [GATE_RESULTS_DIR]: ['gate-results'],
  [RECORDINGS_DIR]: ['verify/recordings', 'recordings'],
  [PLAY_REPORT]: ['play/agent-play-report.json'],
  [PLAY_HISTORY]: ['play/agent-play-history.jsonl'],
  [TEST_RESULTS]: ['test-run/test-results.json', 'test-results.json'],
  [VIDEO_ANALYSIS]: ['verify/video/video-analysis.json', 'video/video-analysis.json', 'video-analysis.json'],
  [VISUAL_ALIGNMENT]: ['alignment/visual/alignment.json', 'visual-alignment/alignment.json'],
  [GAMEPLAY_ALIGNMENT]: ['alignment/gameplay/alignment.json', 'gameplay-alignment/alignment.json'],
  [CRITIC_REPORT]: ['critic/critic-report.json', 'critic-report.json'],
  [ASSET_INTEGRATION_REPORT]: ['assets/asset-integration-report.json', 'asset-integration-report.json'],
  [SLOT_VALIDATION_DIR]: ['verify/assets/slot'],
  [ASSET_SMOKE_REPORT]: ['asset-smoke/asset-smoke-report.json'],
  [PREFLIGHT_REPORT]: ['preflight-report.json'],
};

/**
 * Resolve an artifact path under `playtestDir`, preferring the new layout but
 * falling back to legacy locations. Returns the canonical (new) path if
 * nothing exists yet.
 */
export function resolveRead(playtestDir, rel) {
  const primary = join(playtestDir, rel);
  if (existsSync(primary)) return primary;
  for (const legacy of LEGACY[rel] || []) {
    const candidate = join(playtestDir, legacy);
    if (existsSync(candidate)) return candidate;
  }
  return primary;
}
