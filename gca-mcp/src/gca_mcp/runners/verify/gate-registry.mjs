/**
 * gate-registry.mjs — Single source of the gate name -> checks mapping.
 *
 * Owns the composite check lists (iterationGateChecks / firstPassGateChecks /
 * releaseGateChecks) and the `runGate(gate)` dispatch that the thin
 * gate-check.mjs CLI shim invokes. The individual gate* implementations live in
 * gate-engine.mjs; run state (args / paths / failures) lives in gate-context.mjs.
 *
 * Behaviour is identical to the former in-file dispatch: composite gates run
 * their check lists (accumulating BLOCKED messages via the engine's fail()),
 * the JSON-emitting gates (build_digest / gate_contract /
 * materialize_layout_alignment) print and exit 0, and an unknown gate exits 2.
 */

import { join } from 'path';
import {
  args,
  playtestDir,
  configPath,
  failures,
  blockingIssues,
  fail,
} from './gate-context.mjs';
import { LAYOUT_ALIGNMENT } from './playtest-layout.mjs';
import { readJSON } from './shared/gate-io.mjs';
import { metadataFor } from './gate-metadata.mjs';
import {
  canonicalBuildDigest,
  gateBasicStructure,
  gateStateJson,
  gateTestsExist,
  gateRubricContract,
  gateAssetManifest,
  gateAssetRuntimeReferences,
  gateScenesImplemented,
  gateAssetsGenerated,
  gateTestsPassing,
  gateNoOpenIssues,
  gateVisualVerified,
  gatePlaythroughRecorded,
  gateReleaseWaivers,
  gateVerifyReport,
  gateCriticReport,
  gateGameplayAlignment,
  gatePhysicsAlignment,
  gateVisualAlignment,
  gateAssetIntegrationReport,
  gatePerformance,
  gatePolishRounds,
  gateProjectedContracts,
  gateCanonicalHelpers,
  gateLevelsPresent,
  gateLevelRuntime,
  gateTestCoverage,
  gatePlayQuality,
  gateBboxCoverage,
  gateCropCoverage,
  gateAspectDrift,
  gateMattingComplete,
  gateElementAlignment,
  gateUiLayout,
  gateSceneCoverage,
  gateVisualDiff,
  materializeLayoutAlignment,
  gateLayoutAlignment,
  gateUiProportion,
  gateUiContract,
  gateNoFxSpritesheets,
  gateSubjectMargin,
  gateEntityScale,
  gateSlotValidation,
  gateTowerDefenseContract,
  gateInteractionContract,
} from './gate-engine.mjs';

// ── Composite check lists ────────────────────────────────────────────────
// Each entry is [checkName, checkFn]. The names are the authoritative BLOCKED
// grouping keys and drive the gate_contract self-report; see gate-metadata.mjs
// for the per-check axis/agent routing annotations.

export function iterationGateChecks(state, allowedModes = null) {
  return [
    ['basic_structure', gateBasicStructure],
    ['levels', () => gateLevelsPresent(state)],
    ['level_runtime', () => gateLevelRuntime(state)],
    ['test_contract', gateTestsExist],
    ['rubric_contract', gateRubricContract],
    ['asset_manifest', gateAssetManifest],
    ['asset_runtime_references', gateAssetRuntimeReferences],
    ['scenes_implemented', () => gateScenesImplemented(state)],
    ['assets_generated', () => gateAssetsGenerated(state)],
    ['tests_passing', () => gateTestsPassing(state)],
    ['visual_verified', () => gateVisualVerified(state)],
    ['asset_integration', gateAssetIntegrationReport],
    ['verify_report', () => gateVerifyReport(allowedModes)],
    ['visual_alignment', gateVisualAlignment],
    ['gameplay_alignment', gateGameplayAlignment],
    ['physics_alignment', gatePhysicsAlignment],
    ['test_coverage', () => gateTestCoverage(state)],
    ['bbox_coverage', gateBboxCoverage],
    ['crop_coverage', gateCropCoverage],
    ['aspect_drift', gateAspectDrift],
    ['matting_complete', gateMattingComplete],
    ['no_fx_spritesheets', gateNoFxSpritesheets],
    ['subject_margin', gateSubjectMargin],
    ['interaction_contract', gateInteractionContract],
    ['slot_validation', () => gateSlotValidation(true)],
    ['ui_contract', gateUiContract],
    ['ui_layout', gateUiLayout],
    ['element_alignment', gateElementAlignment],
    ['visual_diff', gateVisualDiff],
    ['scene_coverage', gateSceneCoverage],
    ['layout_alignment', gateLayoutAlignment],
    ['tower_defence_contract', gateTowerDefenseContract],
  ];
}

export function firstPassGateChecks(state) {
  return [
    ...iterationGateChecks(state, ['full', 'full-novideo']),
    ['release_waivers', () => gateReleaseWaivers(state)],
    ['playthrough_recorded', () => gatePlaythroughRecorded(state)],
    ['performance', gatePerformance],
  ];
}

export function releaseGateChecks(state) {
  return [
    ...iterationGateChecks(state, ['full', 'full-novideo']),
    ['release_waivers', () => gateReleaseWaivers(state)],
    ['playthrough_recorded', () => gatePlaythroughRecorded(state)],
    ['performance', gatePerformance],
    ['no_open_issues', () => gateNoOpenIssues(state)],
    ['critic', gateCriticReport],
    ['polish_rounds', gatePolishRounds],
    ['play_quality', gatePlayQuality],
  ];
}

export function runChecks(checks) {
  for (const [name, check] of checks) {
    const before = failures.length;
    check();
    // Attribute every failure this check produced to its name + routing
    // metadata (P3 structured channel). The stderr BLOCKED lines are emitted
    // verbatim by the shim regardless, so this is purely additive.
    for (let i = before; i < failures.length; i += 1) {
      blockingIssues.push({
        check: name,
        ...metadataFor(name),
        message: failures[i].replace(/^BLOCKED:\s*/, ''),
      });
    }
  }
}

// ── Gate dispatch ──────────────────────────────────────────────────────
// Runs the requested gate. Composite gates accumulate failures via the engine's
// fail(); the JSON-emitting gates print + exit 0; an unknown gate exits 2. The
// thin gate-check.mjs shim reads the accumulated failures afterward and emits
// the human report + structured payload.

export function runGate() {
  const gate = args.gate;

  if (gate === 'scaffold_done') {
    let state = null;
    runChecks([
      ['basic_structure', gateBasicStructure],
      ['pipeline_state', () => { state = gateStateJson(true); }],
      ['levels', () => gateLevelsPresent(state)],
      ['scaffold_contracts', () => {
        gateProjectedContracts();
        gateCanonicalHelpers();
      }],
      ['test_contract', gateTestsExist],
      ['rubric_contract', gateRubricContract],
      ['asset_manifest', gateAssetManifest],
    ]);
  }

  else if (gate === 'step_done') {
    const step = Number(args.step);
    if (!Number.isInteger(step) || step < 2 || step > 7) {
      fail('step_done requires an integer --step from 2 through 7 under the current pipeline numbering');
      return;
    }
    let state = null;
    const checks = [
      ['basic_structure', gateBasicStructure],
      ['pipeline_state', () => { state = gateStateJson(true); }],
    ];
    if (step >= 2) {
      checks.push(
        ['levels', () => gateLevelsPresent(state)],
        ['scaffold_contracts', () => {
          gateProjectedContracts();
          gateCanonicalHelpers();
        }],
        ['test_contract', gateTestsExist],
        ['rubric_contract', gateRubricContract],
        ['asset_manifest', gateAssetManifest],
      );
    }
    if (step >= 3) {
      checks.push(
        ['level_runtime', () => gateLevelRuntime(state)],
        ['scenes_implemented', () => gateScenesImplemented(state)],
        ['assets_generated', () => gateAssetsGenerated(state)],
      );
    }
    if (step >= 4) {
      checks.push(
        ['asset_integration', () => gateAssetIntegrationReport({
          requireFinalizedSeal: step >= 7,
        })],
        ['entity_scale', gateEntityScale],
        ['ui_proportion', gateUiProportion],
        ['slot_validation', () => gateSlotValidation(
          true,
          { requireFinalizedSeal: step >= 7 },
        )],
      );
    }
    if (step >= 6) checks.push(['visual_verified', () => gateVisualVerified(state)]);
    if (step >= 7) {
      checks.push(
        ['release_waivers', () => gateReleaseWaivers(state)],
        ['playthrough_recorded', () => gatePlaythroughRecorded(state)],
        ['verify_report', () => gateVerifyReport(['full', 'full-novideo'])],
        ['performance', gatePerformance],
      );
    }
    runChecks(checks);
  }

  else if (gate === 'first_pass_complete') {
    const state = gateStateJson(true);
    runChecks(firstPassGateChecks(state));
  }

  // Iteration gate — the NO-VIDEO sibling of first_pass_complete, run every fix
  // round. Identical to first_pass_complete EXCEPT it omits the two gates that need
  // the (expensive) recording pipeline: gatePlaythroughRecorded + gatePerformance.
  // The fix→re-verify loop grinds on this cheap gate (screenshots + visual/element
  // image-diff + gameplay/layout + all deterministic axes) up to pipeline.max_iterations.
  // Only after THIS passes does the orchestrator run MODE=full (record + analyze_video
  // + performance) and ready_to_ship. Full is release-only rather than per-round;
  // it must run again only when full-only remediation or a changed build makes the
  // prior release evidence stale. Every correctness axis a fixer can address is
  // still enforced here—only release evidence capture is deferred.
  else if (gate === 'iteration_ready') {
    const state = gateStateJson(true);
    runChecks(iterationGateChecks(state, ['iterate']));
  }

  else if (gate === 'play_quality') {
    gatePlayQuality();
  }

  else if (gate === 'td_contract') {
    gateTowerDefenseContract();
  }

  else if (gate === 'interaction_contract') {
    gateInteractionContract();
  }

  else if (gate === 'ui_contract') {
    gateUiContract();
  }

  else if (gate === 'layout_alignment') {
    gateLayoutAlignment();
  }

  else if (gate === 'materialize_layout_alignment') {
    const result = materializeLayoutAlignment({ blockOnWriteError: false });
    console.log(JSON.stringify({
      gate: 'materialize_layout_alignment',
      alignment_path: join(playtestDir, LAYOUT_ALIGNMENT),
      blocked: (result?.blocking_messages || []).length > 0,
      ...result,
    }, null, 2));
    process.exit(0);
  }

  else if (gate === 'visual_diff') {
    gateVisualDiff();
  }

  else if (gate === 'tests_passing') {
    gateTestsPassing(readJSON(join(playtestDir, 'state.json')));
  }

  else if (gate === 'build_digest') {
    console.log(JSON.stringify({ build_digest: canonicalBuildDigest(), config: configPath }));
    process.exit(0);
  }

  else if (gate === 'gate_contract') {
    const iteration = iterationGateChecks({}).map(([name]) => name);
    const release = releaseGateChecks({}).map(([name]) => name);
    console.log(JSON.stringify({
      iteration_ready: iteration,
      ready_to_ship: release,
      strict_superset: (
        release.length > iteration.length
        && iteration.every((name) => release.includes(name))
      ),
    }));
    process.exit(0);
  }

  else if (gate === 'test_contract') {
    gateTestsExist();
  }

  else if (gate === 'rubric_contract') {
    gateRubricContract();
  }

  else if (gate === 'scaffold_contracts') {
    gateProjectedContracts();
    gateCanonicalHelpers();
  }

  else if (gate === 'levels') {
    let state = null;
    runChecks([
      ['pipeline_state', () => { state = gateStateJson(); }],
      ['levels', () => gateLevelsPresent(state)],
      ['level_runtime', () => gateLevelRuntime(state)],
    ]);
  }

  else if (gate === 'polish_rounds') {
    gatePolishRounds();
  }

  else if (gate === 'verify_report') {
    gateVerifyReport();
  }

  else if (gate === 'critic') {
    gateCriticReport();
  }

  else if (gate === 'crop_coverage') {
    gateCropCoverage();
  }

  else if (gate === 'scene_coverage') {
    gateSceneCoverage();
  }

  else if (gate === 'performance') {
    gatePerformance();
  }

  else if (gate === 'asset_manifest') {
    gateAssetManifest();
  }

  else if (gate === 'asset_runtime_references') {
    gateAssetManifest();
    gateAssetRuntimeReferences();
  }

  else if (gate === 'entity_scale') {
    gateEntityScale();
  }

  else if (gate === 'slot_validation') {
    gateSlotValidation(true);
  }

  else if (gate === 'subject_margin') {
    gateSubjectMargin();
  }

  else if (gate === 'ready_to_ship') {
    const state = gateStateJson(true);
    runChecks(releaseGateChecks(state));
  }

  else if (gate === 'ui_layout') {
    gateUiLayout();
  }

  else {
    console.error(`Unknown gate: ${gate}`);
    console.error('Available: scaffold_done, scaffold_contracts, step_done --step N, iteration_ready, first_pass_complete, build_digest, gate_contract, levels, test_contract, rubric_contract, asset_manifest, asset_runtime_references, entity_scale, slot_validation, subject_margin, performance, play_quality, td_contract, interaction_contract, ui_contract, ui_layout, layout_alignment, materialize_layout_alignment, visual_diff, tests_passing, polish_rounds, verify_report, critic, crop_coverage, scene_coverage, ready_to_ship');
    process.exit(2);
  }
}
