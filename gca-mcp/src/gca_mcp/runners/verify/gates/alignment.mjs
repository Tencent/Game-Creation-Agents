/** Alignment gates — mechanically extracted gate domain. */

import {
  readdirSync,
  statSync,
} from 'fs';

import {
  join,
  resolve,
} from 'path';

import {
  resolveRead,
  VISUAL_ALIGNMENT,
  GAMEPLAY_ALIGNMENT,
  ELEMENT_ALIGNMENT,
  PHYSICS_ALIGNMENT,
  LAYOUT_CONTRACT,
  VISUAL_DIFF_REPORT,
} from '../playtest-layout.mjs';

import { readJSON } from '../shared/gate-io.mjs';
import {
  gameDir,
  playtestDir,
  fail,
  fileExists,
} from '../gate-context.mjs';
import {
  readGcaConfig,
  hasConceptMockups,
  latestSrcMtime,
  qualityProfileName,
  qualityPolicy,
  sceneWaiverState,
  latestVisualInputMtime,
  isPhysicsGame,
} from './support.mjs';

function readAlignment(rel) {
  const p = resolveRead(playtestDir, rel);
  if (!fileExists(p)) return { path: p, present: false };
  const r = readJSON(p);
  if (!r) return { path: p, present: true, invalid: true };
  return { path: p, present: true, report: r };
}

function gateGameplayAlignment() {
  // "Plays-like" axis: running game vs GDD §0.2 mechanics contract (always applicable).
  const a = readAlignment(GAMEPLAY_ALIGNMENT);
  if (!a.present) {
    fail('alignment/gameplay/alignment.json missing — verify-agent must check play vs GDD §0.2 mechanics (gameplay axis)');
    return;
  }
  if (a.invalid) { fail('alignment/gameplay/alignment.json is invalid JSON'); return; }
  if (a.report.applicable === false) return;
  if (a.report.aligned === false) {
    const n = Array.isArray(a.report.issues) ? a.report.issues.length : 0;
    fail(`gameplay alignment failed — running game does not match GDD §0.2 mechanics${n ? ` (${n} issue(s))` : ''}`);
  }
  const srcM = latestSrcMtime();
  if (srcM && statSync(a.path).mtimeMs < srcM) {
    fail('alignment/gameplay is stale (older than latest src/ change) — re-run gameplay alignment');
  }
}

function gatePhysicsAlignment() {
  // Physics axis (Axis 4): deterministic body/art, topology, world, target,
  // tunneling, and prefab checks projected by materialize_physics_alignment.
  if (readGcaConfig().gates?.interaction?.physics_alignment === false) return;
  if (!isPhysicsGame()) return; // non-physics → N/A
  const a = readAlignment(PHYSICS_ALIGNMENT);
  if (!a.present) {
    fail('alignment/physics/alignment.json missing although interaction_profile/topology requires physics evidence');
    return;
  }
  if (a.invalid) { fail('alignment/physics/alignment.json is invalid JSON'); return; }
  if (a.report.applicable === false) {
    fail('physics alignment cannot be applicable:false when interaction_profile/topology requires physics evidence');
    return;
  }
  if (a.report.aligned === false) {
    const n = Array.isArray(a.report.issues) ? a.report.issues.length : 0;
    fail(`physics alignment failed — interaction bodies/world contract do not align with visible gameplay${n ? ` (${n} issue(s))` : ''}`);
  }
  const srcM = latestSrcMtime();
  if (srcM && statSync(a.path).mtimeMs < srcM) {
    fail('alignment/physics is stale (older than latest src/ change) — rerun physics materialization');
  }
}

function gateVisualAlignment() {
  // "Looks-like" axis: running screens vs design/<screen>.png concept mockups.
  // Only enforced when concept mockups exist (skipped for no-image-credential text-only games).
  if (readGcaConfig().gates?.visual?.axes?.global === false) return;
  if (!hasConceptMockups()) return;
  const a = readAlignment(VISUAL_ALIGNMENT);
  if (!a.present) {
    fail('alignment/visual/alignment.json missing — concept mockups exist; verify-agent must compare running screens vs design/<screen>.png (visual axis)');
    return;
  }
  if (a.invalid) { fail('alignment/visual/alignment.json is invalid JSON'); return; }
  if (a.report.applicable === false) return;
  if (a.report.aligned === false) {
    const n = Array.isArray(a.report.issues) ? a.report.issues.length : 0;
    fail(`visual alignment failed — running screens do not match the concept mockups${n ? ` (${n} issue(s))` : ''}`);
  }
  const srcM = latestSrcMtime();
  if (srcM && statSync(a.path).mtimeMs < srcM) {
    fail('alignment/visual is stale (older than latest src/ change) — re-run visual alignment');
  }
}
// Gate: per-element alignment (Axis 3) — each §2E UI element's running render vs
// its concept crop (looks-like at element granularity). Mirrors gateVisualAlignment.
function gateElementAlignment() {
  if (readGcaConfig().gates?.visual?.axes?.element === false) return;
  if (!hasConceptMockups()) return; // no concept crops → element axis N/A
  const a = readAlignment(ELEMENT_ALIGNMENT);
  if (!a.present) {
    fail('alignment/element/alignment.json missing — concept mockups exist; verify-agent must compare each §2E UI element (bars/buttons/panels/badges) vs its design crop (element axis): is it real art, correctly sized, matching the concept?');
    return;
  }
  if (a.invalid) { fail('alignment/element/alignment.json is invalid JSON'); return; }
  if (a.report.applicable === false) return;
  if (a.report.aligned === false) {
    const n = Array.isArray(a.report.issues) ? a.report.issues.length : 0;
    fail(`element alignment failed — one or more UI elements do not match their concept crop (crude primitive instead of art, wrong size, or visual mismatch)${n ? ` (${n} issue(s))` : ''}`);
  }
  const srcM = latestSrcMtime();
  if (srcM && statSync(a.path).mtimeMs < srcM) {
    fail('alignment/element is stale (older than latest src/ change) — re-run element alignment');
  }
}
// ── Gate: scene coverage (gateSceneCoverage) ────────────────────────────────
// Every interactive scene declared in the layout contract must have a concept
// mockup (design/<slug>.png). `waived_scenes` excludes either pure transitions
// or an explicit `waived:concept-generation-unavailable` reference fallback.
// The latter still retains a scene layout; only the missing image is exempt.
// N/A when the selected profile does not require references or the layout
// contract is absent/empty.
function gateSceneCoverage() {
  const policy = qualityPolicy();
  if (!policy.requireConceptReference) return;
  const contractPath = resolveRead(playtestDir, LAYOUT_CONTRACT);
  if (!fileExists(contractPath)) return;
  const contract = readJSON(contractPath);
  if (!contract || !contract.scenes || !Object.keys(contract.scenes).length) return;

  const { valid: waived, issues: waiverIssues } = sceneWaiverState(contract);
  for (const issue of waiverIssues) fail(issue);
  const designDir = join(gameDir, 'design');
  let screenSlugs;
  try {
    screenSlugs = new Set(
      readdirSync(designDir)
        .filter((f) => f.toLowerCase().endsWith('.png'))
        .map((f) => f.replace(/\.png$/i, '').toLowerCase().replace(/[^a-z0-9]/g, ''))
    );
  } catch { screenSlugs = new Set(); }

  const normalizeName = (s) => String(s).toLowerCase().replace(/scene$/, '').replace(/[^a-z0-9]/g, '');
  const missing = [];
  for (const [sceneKey, sceneContract] of Object.entries(contract.scenes)) {
    if (waived.has(sceneKey)) continue;
    const slug = normalizeName(sceneKey);
    if (policy.requirePerSceneConcept) {
      if (!screenSlugs.has(slug)) missing.push(sceneKey);
      continue;
    }
    const explicitRef = (
      sceneContract?.reference
      || sceneContract?.mockup
      || sceneContract?.design_ref
    );
    const hasExplicitRef = (
      typeof explicitRef === 'string'
      && fileExists(resolve(gameDir, explicitRef))
    );
    if (!hasExplicitRef && !screenSlugs.has(slug)) missing.push(sceneKey);
  }
  if (missing.length) {
    const expectation = policy.requirePerSceneConcept
      ? 'a distinct design/<scene>.png'
      : 'an explicit scene reference mapping (shared references are allowed)';
    fail(`scene coverage gap (${qualityProfileName()}) — ${missing.length} interactive scene(s) lack ${expectation}: ${missing.join(', ')}. Waive only pure transitions or a recorded concept-generation-unavailable fallback.`);
  }
}

// ── Gate: comprehensive visual diff (gateVisualDiff) ───────────────────────
// The "comprehensive visual verification (global + per-element)" enforcement.
// element-visual-diff.mjs backs the
// visual axes with a REAL image comparison — the running screen vs design/<screen>.png
// (global) and each rendered element region vs its design/crops/<screen>/<crop>.png
// (per-element, paired VLM) — and writes a deterministic sidecar. This gate REQUIRES
// that sidecar so the visual axes can no longer pass on a hand-authored bool: the loop
// must actually have run, be aligned, and be fresh vs src/. It iterates on its own
// budget (gates.visual.max_rounds) — independent of pipeline.max_iterations. N/A when
// disabled or when there are no concept mockups. Provider exhaustion is explicit
// status:"unavailable" and follows the repository's warning-only outage policy.
function gateVisualDiff() {
  const vcfg = readGcaConfig().gates?.visual || {};
  if (vcfg.enabled === false) return;              // explicit opt-out
  if (vcfg.require_report === false) return;       // sidecar not required
  if (vcfg.axes?.global === false && vcfg.axes?.element === false) return;
  if (!hasConceptMockups()) return;                // no mockups → visual axis N/A
  const p = resolveRead(playtestDir, VISUAL_DIFF_REPORT);
  if (!fileExists(p)) {
    fail('verify_middle/visual/visual-diff-report.json missing — concept mockups exist; run element-visual-diff.mjs (comprehensive visual verification: whole-screen + per-element image diff) so the visual axes are backed by a real comparison, not a hand-authored bool.');
    return;
  }
  const r = readJSON(p);
  if (!r) { fail('visual-diff-report.json is invalid JSON'); return; }

  // Anti-fabrication: the comprehensive visual axes must come from the bundled
  // script, not a hand-authored JSON summary. The script writes source-tagged
  // alignment files and an evidence-backed sidecar with screen/render PNGs. N/A
  // and provider-unavailable reports must retain the same provenance.
  const visual = readJSON(resolveRead(playtestDir, VISUAL_ALIGNMENT));
  const element = readJSON(resolveRead(playtestDir, ELEMENT_ALIGNMENT));
  const globalEnabled = r.axes?.global !== false;
  const elementEnabled = r.axes?.element !== false;
  const sourceOk = (x, enabled) => !enabled || (
    x && x.source === 'element-visual-diff.mjs' && x.run_id && x.run_id === r.run_id
  );
  if (r.source !== 'element-visual-diff.mjs' || !r.run_id ||
      !sourceOk(visual, globalEnabled) || !sourceOk(element, elementEnabled)) {
    fail('visual diff provenance invalid — sidecar and enabled visual/element alignments must share source:"element-visual-diff.mjs" and one run_id; hand-authored or mixed-run reports are not accepted.');
  }
  if (/manual/i.test(String(r.method || '')) || r.global || r.element || r.summary) {
    fail('visual-diff-report.json looks hand-authored/manual rather than produced by element-visual-diff.mjs — run the bundled runner and keep its paired_vlm/concept_crop sidecar schema.');
  }
  if (r.method && r.method !== 'paired_vlm') {
    fail(`visual-diff-report.json method "${r.method}" is not the required paired_vlm method from element-visual-diff.mjs.`);
  }
  if (r.reference && r.reference !== 'concept_crop') {
    fail(`visual-diff-report.json reference "${r.reference}" is not the required concept_crop reference from element-visual-diff.mjs.`);
  }
  if (r.applicable === false) {
    fail('visual-diff-report.json is not applicable even though concept mockups and at least one visual axis are enabled.');
    return;
  }

  const status = r.status;
  const elements = Array.isArray(r.scenes) ? r.scenes.flatMap((s) => Array.isArray(s.elements) ? s.elements : []) : [];
  const comparisons = Array.isArray(r.scenes) ? r.scenes.flatMap((s) => [
    ...(s?.global ? [s.global] : []),
    ...(Array.isArray(s?.elements) ? s.elements : []),
  ]) : [];
  const hasFalse = comparisons.some((entry) => entry?.match === false);
  const inconclusive = comparisons.filter((entry) => entry && (entry.error || typeof entry.match !== 'boolean'));
  const visualInputMtime = latestVisualInputMtime();
  for (const [label, evidencePath] of [
    ['sidecar', p],
    ['visual alignment', resolveRead(playtestDir, VISUAL_ALIGNMENT)],
    ['element alignment', resolveRead(playtestDir, ELEMENT_ALIGNMENT)],
  ]) {
    if (fileExists(evidencePath) && visualInputMtime && statSync(evidencePath).mtimeMs < visualInputMtime) {
      fail(`${label} is stale relative to source/assets/design/layout/config inputs — rerun visual_diff`);
    }
  }

  if (!['passed', 'failed', 'unavailable', 'error'].includes(status)) {
    fail(`visual diff status "${status}" is invalid`);
  }
  if (status === 'error') {
    fail(`visual diff execution failed — ${r.reason || 'capture/script error'}; rerun after fixing the verifier.`);
    return;
  }
  if (status === 'unavailable') {
    if (r.aligned !== null) {
      fail('visual diff unavailable report is invalid — aligned must be null, never true/false.');
    }
    if (hasFalse) {
      fail('visual diff unavailable report contains a real match:false verdict — real visual mismatches must remain status:"failed" and block.');
    }
    if (!r.reason && !r.unavailable_reason && inconclusive.length === 0) {
      fail('visual diff unavailable report has no provider error/reason.');
    }
    console.warn(`⚠️ Visual diff unavailable after bounded retries; continuing because unavailable_policy=warn (${r.reason || `${r.unavailable_count || 0} comparison(s)`}).`);
    return;
  }
  if (status === 'passed' && (r.aligned !== true || hasFalse || inconclusive.length > 0)) {
    fail('visual diff status:"passed" is inconsistent with aligned/comparison verdicts.');
  }
  if (status === 'failed' && (r.aligned !== false || !hasFalse)) {
    fail('visual diff status:"failed" must have aligned:false and at least one real match:false verdict.');
  }

  const coverage = r.coverage || {};
  if (globalEnabled && (
    !Number.isFinite(Number(coverage.expected_scenes)) ||
    Number(coverage.expected_scenes) <= 0 ||
    Number(coverage.captured_scenes) !== Number(coverage.expected_scenes) ||
    Number(coverage.compared_global) !== Number(coverage.expected_scenes)
  )) {
    fail('visual diff global coverage is incomplete for the current run.');
  }
  if (elementEnabled && (
    !Number.isFinite(Number(coverage.expected_elements)) ||
    Number(coverage.compared_elements) !== Number(coverage.expected_elements)
  )) {
    fail('visual diff element coverage is incomplete for the current run.');
  }
  if ((globalEnabled ? Number(coverage.compared_global || 0) : 0) +
      (elementEnabled ? Number(coverage.compared_elements || 0) : 0) <= 0) {
    fail('visual diff produced zero comparisons for enabled axes.');
  }

  if (!r.scene_screen_map || typeof r.scene_screen_map !== 'object') {
    fail('visual-diff-report.json missing scene_screen_map — the sidecar must include real .screen.png evidence paths from element-visual-diff.mjs.');
  } else {
    // element-visual-diff.mjs keys scene_screen_map by SCENE name and writes each
    // global screenshot to <VISUAL_DIFF_DIR>/<sceneKey>.screen.png (see that
    // script's capture()). The map VALUE is the design-screen alias (title /
    // gameplay / gameover), NOT a file path — so resolve evidence by the scene KEY
    // against the sidecar's own directory. (A value that is itself an existing
    // relative path is still honored, for forward-compat.)
    const visualDiffDir = p.replace(/\/[^/]+$/, '');
    const missingScreens = [];
    for (const [sceneKey, alias] of Object.entries(r.scene_screen_map)) {
      const byKey = join(visualDiffDir, `${sceneKey}.screen.png`);
      const byValue = alias ? join(gameDir, String(alias)) : null;
      const evidencePath = fileExists(byKey) ? byKey : (byValue && fileExists(byValue) ? byValue : null);
      if (!evidencePath || (visualInputMtime && statSync(evidencePath).mtimeMs < visualInputMtime)) {
        missingScreens.push(String(alias || sceneKey || '<missing>'));
      }
    }
    if (missingScreens.length) {
      fail(`visual diff screen evidence missing — ${missingScreens.slice(0, 6).join(', ')}${missingScreens.length > 6 ? ', …' : ''}. Run element-visual-diff.mjs; do not hand-write the sidecar.`);
    }
  }
  if (inconclusive.length) {
    fail(`visual diff has ${inconclusive.length} inconclusive VLM comparison(s) but status is "${status}" — emit status:"unavailable" instead of treating null/error as aligned.`);
  }
  const missingRenders = [];
  for (const e of elements) {
    const rel = e && e.running;
    const renderPath = rel ? join(gameDir, rel) : null;
    if (!renderPath || !fileExists(renderPath) ||
        (visualInputMtime && statSync(renderPath).mtimeMs < visualInputMtime)) {
      missingRenders.push(String(rel || e?.key || e?.label || '<missing>'));
    }
  }
  const elementChecked = Array.isArray(element?.checked) ? element.checked : [];
  if (r.axes?.element !== false && elementChecked.length > 0 && elements.length === 0) {
    fail('visual diff element evidence missing — element alignment checked entries exist, but sidecar has no per-element entries/render PNGs; run element-visual-diff.mjs instead of hand-authoring element alignment.');
  } else if (missingRenders.length) {
    fail(`visual diff render evidence missing — ${missingRenders.slice(0, 8).join(', ')}${missingRenders.length > 8 ? ', …' : ''}. Each element entry must reference an existing .render.png produced by element-visual-diff.mjs.`);
  }

  if (r.aligned === false) {
    const g = r.global_aligned === false ? ' global' : '';
    const e = r.element_aligned === false ? ' element' : '';
    fail(`visual diff failed —${g || e ? `${g}${e} axis` : ' one or more axes'} do not match the concept mockups/crops (see verify_middle/visual/visual-diff-report.json + alignment/{visual,element}). Route: placeholder / wrong art → assets-agent or design-agent; visible layout / value-anchor / slot-icon mismatch → implement-agent. Runtime roster count and action wiring are enforced separately by gateUiBinding. Iterate the visual loop up to gates.visual.max_rounds.`);
  }
  const srcM = latestSrcMtime();
  if (srcM && statSync(p).mtimeMs < srcM) {
    fail('visual-diff-report.json is stale (older than latest src/ change) — re-run element-visual-diff.mjs (the visual fix→re-verify loop).');
  }
}

export {
  gateGameplayAlignment,
  gatePhysicsAlignment,
  gateVisualAlignment,
  gateElementAlignment,
  gateSceneCoverage,
  gateVisualDiff,
} ;
