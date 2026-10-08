"""Canonical ``.playtest/`` directory layout — single source of truth.

Every per-game verification artifact lives under ``<GAME_DIR>/.playtest/``. This
module defines WHERE each artifact goes so producers (tools/scripts) and
consumers (``write_verify_report``, gates, hooks) never drift.

Design: keep the top level tiny. ALL verification-check artifacts live under a
single ``verify/`` folder; only the pipeline state machine, test sources, and
pre-pipeline checks sit above it.

    .playtest/
      state.json · resume.json · audit.jsonl   pipeline state machine (root)
      tests/                                    test SOURCES (inputs)
      inputs/                                   authored inputs / intermediates
        rubric.json · display-params.json
        interaction-contract.json · objectives.json · layout-contract.json  contract (scaffold-authored)
        interactions/<scenario>.mjs             one interaction script per scenario
        scenarios/<scenario>.json               optional seed states (record_with_state)
      preflight/                                pre-pipeline checks
        preflight-report.json + rmbg-smoke.png
        asset-smoke-report.json + seedance-reference.png
      verify/                                   ALL verification outputs
        verify-report.json                      aggregate entry point
        checkpoints/<producer>.json             identity-bound producer progress
        gate-results/                           gate_check outputs
        recording/<scenario>/                   PER-SCENARIO capture + analysis:
                    recording.webm + state-timeline.json + performance-summary.json
                    + video-analysis.json + evidence/
        test-run/   test-results.json + screenshots/ + recordings/
        play/       agent-play-report.json + agent-play-history.jsonl + trajectory.jsonl + frames/
        alignment/  visual/ + gameplay/ + element/ + physics/ + layout/  (alignment.json each)
        critic/     critic-report.json
        assets/     asset-integration-report.json
      verify_middle/                            validation INTERMEDIATES (kept OUT of verify/)
        slot/<key>.json                         (b) per-entity slot-fit verdicts (assets-agent)
        scale/scale-report.json                 (a) entity-scale audit (gateEntityScale)
        layout/<scene>.rendered.json            (Axis 5) per-scene rendered element bounds
        visual/visual-diff-history.jsonl        append-only counted visual runs

Artifacts were relocated across two earlier layouts (a flat one, then a
per-stage one). To keep migrations safe, :func:`resolve_read` prefers the
current path but falls back through the older locations, so old games and
partially-migrated runs still validate.
"""

from __future__ import annotations

from pathlib import Path

# ── Root: pipeline state machine (above the verify/ umbrella) ─────────────
STATE = "state.json"
RESUME = "resume.json"
AUDIT = "audit.jsonl"

# ── Top-level groups ──────────────────────────────────────────────────────
TESTS_DIR = "tests"  # test SOURCES (inputs, not outputs)
INPUTS_DIR = "inputs"  # authored inputs / intermediates the tools read
PREFLIGHT_DIR = "preflight"  # pre-pipeline checks (env + asset smoke)
VERIFY_DIR = "verify"  # umbrella for ALL verification outputs
# Dedicated home for validation INTERMEDIATES (slot-fit verdicts, raw VLM
# replies, checked-frame snapshots). Deliberately a SEPARATE top-level dir from
# verify/ so this flow never overlaps the canonical verification outputs.
VERIFY_MIDDLE_DIR = "verify_middle"

# ── Authored inputs / intermediates (under inputs/) ───────────────────────
RUBRIC = f"{INPUTS_DIR}/rubric.json"  # quality bar (verify/critic input)
# Interaction contract authored by scaffold-agent from GDD §0.2 — the unified
# SSOT for player input (`actions`) AND collision topology (`layers`/`matrix`/
# `world`/`character`/`entities`). Renamed + extended from the old
# ``control-map.json``; consumed by play_game (reads `.actions`) and the
# ``interaction_contract`` gate. CONTROL_MAP stays as a transition alias.
INTERACTION_CONTRACT = f"{INPUTS_DIR}/interaction-contract.json"
CONTROL_MAP = (
    INTERACTION_CONTRACT  # transition alias — remove once all refs use the new name
)
OBJECTIVES = f"{INPUTS_DIR}/objectives.json"
# Scene layout contract authored by scaffold-agent from GDD §2.0.2 — the
# lays-out-like SSOT for element POSITION (per scene: key + box[x,y,w,h] (0..1) +
# origin + anchorTo + kind). Consumed by the gateLayoutAlignment check, which
# compares it against the rendered bounds dump (LAYOUT_RENDERED_DIR).
LAYOUT_CONTRACT = f"{INPUTS_DIR}/layout-contract.json"
# Interaction scripts — ONE PER SCENARIO (early-game / boss-fight / near-death / …),
# each driving its own recording. See the coverage matrix in verify-agent.md.
INTERACTIONS_DIR = f"{INPUTS_DIR}/interactions"  # inputs/interactions/<scenario>.mjs
SCENARIOS_DIR = (
    f"{INPUTS_DIR}/scenarios"  # optional seed JSON: inputs/scenarios/<scenario>.json
)
INTERACT_SCRIPT = f"{INPUTS_DIR}/interact.mjs"  # legacy single-script (back-compat)
DISPLAY_PARAMS = f"{INPUTS_DIR}/display-params.json"  # integration-owned entity sizing

# ── Verification sub-directories (under verify/) ──────────────────────────
GATE_RESULTS_DIR = f"{VERIFY_DIR}/gate-results"
VERIFY_CHECKPOINTS_DIR = f"{VERIFY_DIR}/checkpoints"
# Recordings are PER-SCENARIO: verify/recording/<scenario>/ holds that scenario's
# capture (recording.webm + state-timeline.json + performance-summary.json) AND its
# analysis (video-analysis.json + evidence/). RECORDING_DIR is the parent.
RECORDING_DIR = f"{VERIFY_DIR}/recording"
RECORDINGS_DIR = RECORDING_DIR  # back-compat alias (parent scanned for *.webm)
VIDEO_DIR = RECORDING_DIR  # back-compat alias
RUNTIME_DIR = f"{VERIFY_DIR}/runtime"
PLAY_DIR = f"{VERIFY_DIR}/play"
TEST_RUN_DIR = f"{VERIFY_DIR}/test-run"
ALIGNMENT_DIR = f"{VERIFY_DIR}/alignment"
CRITIC_DIR = f"{VERIFY_DIR}/critic"
ASSETS_DIR = f"{VERIFY_DIR}/assets"

# ── Canonical artifact paths (relative to <GAME_DIR>/.playtest) ───────────────
VERIFY_REPORT = f"{VERIFY_DIR}/verify-report.json"
VERIFY_RUN = f"{VERIFY_DIR}/current-run.json"
RUNTIME_TOKEN_RECEIPT = f"{RUNTIME_DIR}/runtime-token.json"
RUNTIME_ASSETS_RECEIPT = f"{RUNTIME_DIR}/runtime-assets.json"
VIDEO_RECEIPT_NAME = "video-receipt.json"

# Pre-pipeline checks (preflight + asset smoke share one folder)
PREFLIGHT_REPORT = f"{PREFLIGHT_DIR}/preflight-report.json"
ASSET_SMOKE_REPORT = f"{PREFLIGHT_DIR}/asset-smoke-report.json"
ASSET_SMOKE_DIR = PREFLIGHT_DIR

TEST_RESULTS = f"{TEST_RUN_DIR}/test-results.json"
TEST_SCREENSHOTS_DIR = f"{TEST_RUN_DIR}/screenshots"
TEST_RECORDINGS_DIR = f"{TEST_RUN_DIR}/recordings"

VIDEO_ANALYSIS = f"{RECORDING_DIR}/video-analysis.json"
VIDEO_EVIDENCE_DIR = f"{RECORDING_DIR}/evidence"

PLAY_REPORT = f"{PLAY_DIR}/agent-play-report.json"
PLAY_HISTORY = f"{PLAY_DIR}/agent-play-history.jsonl"

VISUAL_ALIGNMENT = f"{ALIGNMENT_DIR}/visual/alignment.json"
GAMEPLAY_ALIGNMENT = f"{ALIGNMENT_DIR}/gameplay/alignment.json"
# Per-element alignment axis (Axis 3): each §2E UI element's running render vs its
# concept crop (looks-like at element granularity). Sibling of visual/gameplay.
ELEMENT_ALIGNMENT = f"{ALIGNMENT_DIR}/element/alignment.json"
# Physics alignment axis (Axis 4): deterministic interaction body/art/world
# checks projected into the verify epoch. Sibling of visual/gameplay/element.
PHYSICS_ALIGNMENT = f"{ALIGNMENT_DIR}/physics/alignment.json"
# Layout alignment axis (Axis 5): the running game's measured element positions
# (LAYOUT_RENDERED_DIR) vs the §2.0.2 layout-contract.json. WRITTEN by
# materialize_layout_alignment; gateLayoutAlignment later consumes it for the
# blocking decision. Sibling of the other alignment axes.
LAYOUT_ALIGNMENT = f"{ALIGNMENT_DIR}/layout/alignment.json"

CRITIC_REPORT = f"{CRITIC_DIR}/critic-report.json"
ASSET_INTEGRATION_REPORT = f"{ASSETS_DIR}/asset-integration-report.json"

# ── Validation intermediates (under verify_middle/, NOT verify/) ───────────
# Per-entity slot-fit verdicts (assets-agent runs validate_asset_slot per sprite
# entity → one <key>.json each, so parallel asset agents never clobber a shared
# aggregate). Lives under verify_middle/ to stay out of the verify/ output tree.
# The scale gate (gateEntityScale) reads display-params.json directly.
SLOT_VALIDATION_DIR = f"{VERIFY_MIDDLE_DIR}/slot"
# (a) Deterministic entity-scale audit WRITTEN by the gate (gateEntityScale):
# per-entity rendered px + per-check verdicts. Lives in the same verify_middle/
# home as slot/ so both asset↔implement coherence checks land together.
SCALE_VALIDATION_DIR = f"{VERIFY_MIDDLE_DIR}/scale"
SCALE_REPORT = f"{SCALE_VALIDATION_DIR}/scale-report.json"
# UI proportion audit WRITTEN by the gate (gateUiProportion): per-element displayed
# px, canvas fraction, texture-downscale + measured-proportion verdicts. Same
# verify_middle/ home as scale/ and slot/ (the asset↔implement coherence checks).
UI_SCALE_VALIDATION_DIR = f"{VERIFY_MIDDLE_DIR}/ui-scale"
UI_SCALE_REPORT = f"{UI_SCALE_VALIDATION_DIR}/ui-scale-report.json"
# (Axis 5) Per-scene rendered element bounds dumped by the Playwright bounds
# capture (named-object getBounds per scene → <scene>.rendered.json). The
# MEASURED side of the layout axis; materialize_layout_alignment reads it +
# LAYOUT_CONTRACT and writes LAYOUT_ALIGNMENT. Same verify_middle/ home as
# scale/ and ui-scale/.
LAYOUT_RENDERED_DIR = f"{VERIFY_MIDDLE_DIR}/layout"
# Comprehensive VISUAL verification (element-visual-diff.mjs): per-scene running
# screenshots + per-element rendered crops + the deterministic sidecar gateVisualDiff
# requires (global + per-element image-diff verdicts). Mirror of playtest-layout.mjs.
VISUAL_DIFF_DIR = f"{VERIFY_MIDDLE_DIR}/visual"
VISUAL_DIFF_REPORT = f"{VISUAL_DIFF_DIR}/visual-diff-report.json"
VISUAL_DIFF_HISTORY = f"{VISUAL_DIFF_DIR}/visual-diff-history.jsonl"

# ── Legacy → backward-compatible reads during/after migration ─────────────
# Maps a canonical path to older locations: [per-stage layout, original flat].
_LEGACY: dict[str, list[str]] = {
    RUBRIC: ["rubric.json"],
    # New name first; fall back to the old control-map.json so existing games resolve.
    INTERACTION_CONTRACT: [
        "inputs/control-map.json",
        "verify/play/control-map.json",
        "play/control-map.json",
    ],
    OBJECTIVES: ["verify/play/objectives.json", "play/objectives.json"],
    INTERACT_SCRIPT: ["interact.mjs"],
    DISPLAY_PARAMS: ["display-params.json"],
    VERIFY_REPORT: ["verify-report.json"],
    GATE_RESULTS_DIR: ["gate-results"],
    RECORDINGS_DIR: ["verify/recordings", "recordings"],
    PLAY_REPORT: ["play/agent-play-report.json"],
    PLAY_HISTORY: ["play/agent-play-history.jsonl"],
    TEST_RESULTS: ["test-run/test-results.json", "test-results.json"],
    VIDEO_ANALYSIS: [
        "verify/video/video-analysis.json",
        "video/video-analysis.json",
        "video-analysis.json",
    ],
    VISUAL_ALIGNMENT: [
        "alignment/visual/alignment.json",
        "visual-alignment/alignment.json",
    ],
    GAMEPLAY_ALIGNMENT: [
        "alignment/gameplay/alignment.json",
        "gameplay-alignment/alignment.json",
    ],
    CRITIC_REPORT: ["critic/critic-report.json", "critic-report.json"],
    ASSET_INTEGRATION_REPORT: [
        "assets/asset-integration-report.json",
        "asset-integration-report.json",
    ],
    SLOT_VALIDATION_DIR: ["verify/assets/slot"],
    ASSET_SMOKE_REPORT: ["asset-smoke/asset-smoke-report.json"],
    PREFLIGHT_REPORT: ["preflight-report.json"],
}


def resolve_read(playtest_dir: Path, rel: str) -> Path:
    """Return the existing artifact path, preferring the new layout.

    Falls back to legacy locations so partially-migrated games keep working.
    If nothing exists yet, returns the canonical (new) path.
    """
    primary = playtest_dir / rel
    if primary.exists():
        return primary
    for legacy in _LEGACY.get(rel, []):
        candidate = playtest_dir / legacy
        if candidate.exists():
            return candidate
    return primary


__all__ = [
    "ALIGNMENT_DIR",
    "ASSETS_DIR",
    "ASSET_INTEGRATION_REPORT",
    "ASSET_SMOKE_DIR",
    "ASSET_SMOKE_REPORT",
    "AUDIT",
    "CONTROL_MAP",
    "CRITIC_DIR",
    "CRITIC_REPORT",
    "DISPLAY_PARAMS",
    "ELEMENT_ALIGNMENT",
    "GAMEPLAY_ALIGNMENT",
    "GATE_RESULTS_DIR",
    "INPUTS_DIR",
    "INTERACTIONS_DIR",
    "INTERACTION_CONTRACT",
    "INTERACT_SCRIPT",
    "LAYOUT_ALIGNMENT",
    "LAYOUT_CONTRACT",
    "LAYOUT_RENDERED_DIR",
    "OBJECTIVES",
    "PHYSICS_ALIGNMENT",
    "PLAY_DIR",
    "PLAY_HISTORY",
    "PLAY_REPORT",
    "PREFLIGHT_DIR",
    "PREFLIGHT_REPORT",
    "RECORDINGS_DIR",
    "RECORDING_DIR",
    "RESUME",
    "RUBRIC",
    "RUNTIME_ASSETS_RECEIPT",
    "RUNTIME_DIR",
    "RUNTIME_TOKEN_RECEIPT",
    "SCALE_REPORT",
    "SCALE_VALIDATION_DIR",
    "SCENARIOS_DIR",
    "SLOT_VALIDATION_DIR",
    "STATE",
    "TESTS_DIR",
    "TEST_RECORDINGS_DIR",
    "TEST_RESULTS",
    "TEST_RUN_DIR",
    "TEST_SCREENSHOTS_DIR",
    "UI_SCALE_REPORT",
    "UI_SCALE_VALIDATION_DIR",
    "VERIFY_CHECKPOINTS_DIR",
    "VERIFY_DIR",
    "VERIFY_MIDDLE_DIR",
    "VERIFY_REPORT",
    "VERIFY_RUN",
    "VIDEO_ANALYSIS",
    "VIDEO_DIR",
    "VIDEO_EVIDENCE_DIR",
    "VIDEO_RECEIPT_NAME",
    "VISUAL_ALIGNMENT",
    "VISUAL_DIFF_DIR",
    "VISUAL_DIFF_HISTORY",
    "VISUAL_DIFF_REPORT",
    "resolve_read",
]
