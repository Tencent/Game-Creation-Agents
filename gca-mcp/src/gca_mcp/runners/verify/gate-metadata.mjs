/**
 * gate-metadata.mjs — Per-check routing annotations for the P3 structured
 * BLOCKED output.
 *
 * Maps each composite check name (the grouping keys in
 * gate-registry.iterationGateChecks / releaseGateChecks) to a coarse `axis`
 * (which verification axis it belongs to) and the `agent` that typically owns
 * the fix. These are ADVISORY hints attached to each structured failure so the
 * main agent can group + route blockers to the right fixer without NLP-parsing
 * the stderr text. They do not affect pass/fail — the deterministic gate logic
 * is unchanged. Unknown check names simply carry no metadata.
 */

export const CHECK_METADATA = {
  basic_structure: { axis: 'structure', agent: 'scaffold-agent' },
  pipeline_state: { axis: 'state', agent: 'orchestrator' },
  levels: { axis: 'levels', agent: 'level-design-agent' },
  level_runtime: { axis: 'code', agent: 'implement-agent' },
  test_contract: { axis: 'tests', agent: 'scaffold-agent' },
  rubric_contract: { axis: 'tests', agent: 'scaffold-agent' },
  asset_manifest: { axis: 'assets', agent: 'scaffold-agent' },
  asset_runtime_references: { axis: 'assets', agent: 'asset-integration-agent' },
  scenes_implemented: { axis: 'code', agent: 'implement-agent' },
  assets_generated: { axis: 'assets', agent: 'assets-agent' },
  tests_passing: { axis: 'tests', agent: 'implement-agent' },
  visual_verified: { axis: 'visual', agent: 'orchestrator' },
  asset_integration: { axis: 'assets', agent: 'asset-integration-agent' },
  verify_report: { axis: 'verify', agent: 'verify-agent' },
  visual_alignment: { axis: 'visual', agent: 'implement-agent' },
  gameplay_alignment: { axis: 'gameplay', agent: 'implement-agent' },
  physics_alignment: { axis: 'physics', agent: 'implement-agent' },
  test_coverage: { axis: 'tests', agent: 'scaffold-agent' },
  bbox_coverage: { axis: 'assets', agent: 'assets-agent' },
  crop_coverage: { axis: 'assets', agent: 'design-agent' },
  aspect_drift: { axis: 'assets', agent: 'assets-agent' },
  matting_complete: { axis: 'assets', agent: 'assets-agent' },
  no_fx_spritesheets: { axis: 'assets', agent: 'assets-agent' },
  subject_margin: { axis: 'assets', agent: 'assets-agent' },
  interaction_contract: { axis: 'interaction', agent: 'implement-agent' },
  slot_validation: { axis: 'assets', agent: 'assets-agent' },
  ui_proportion: { axis: 'ui', agent: 'asset-integration-agent' },
  ui_contract: { axis: 'ui', agent: 'asset-integration-agent' },
  ui_layout: { axis: 'ui', agent: 'implement-agent' },
  element_alignment: { axis: 'element', agent: 'implement-agent' },
  visual_diff: { axis: 'visual', agent: 'implement-agent' },
  scene_coverage: { axis: 'code', agent: 'implement-agent' },
  layout_alignment: { axis: 'layout', agent: 'implement-agent' },
  tower_defence_contract: { axis: 'tower_defence', agent: 'implement-agent' },
  release_waivers: { axis: 'release', agent: 'orchestrator' },
  playthrough_recorded: { axis: 'playthrough', agent: 'verify-agent' },
  performance: { axis: 'performance', agent: 'verify-agent' },
  no_open_issues: { axis: 'release', agent: 'orchestrator' },
  critic: { axis: 'critic', agent: 'critic-agent' },
  polish_rounds: { axis: 'release', agent: 'orchestrator' },
  play_quality: { axis: 'gameplay', agent: 'verify-agent' },

  // ── Standalone gate names used when a failure is produced OUTSIDE runChecks,
  // so the shim's fallback (`check: <gate>`) still resolves axis/agent. These
  // back the direct-dispatch gates and the P4 diagnostic tools —
  // `td_contract` in particular is the gate check_td_contract runs (distinct
  // from the composite check name `tower_defence_contract` above). The
  // composite gate names cover the gateStateJson pre-check failure. ──
  td_contract: { axis: 'tower_defence', agent: 'implement-agent' },
  entity_scale: { axis: 'scale', agent: 'asset-integration-agent' },
  scaffold_done: { axis: 'structure', agent: 'scaffold-agent' },
  scaffold_contracts: { axis: 'structure', agent: 'scaffold-agent' },
  step_done: { axis: 'state', agent: 'orchestrator' },
  iteration_ready: { axis: 'state', agent: 'orchestrator' },
  first_pass_complete: { axis: 'state', agent: 'orchestrator' },
  ready_to_ship: { axis: 'state', agent: 'orchestrator' },
};

export function metadataFor(checkName) {
  return CHECK_METADATA[checkName] || {};
}
