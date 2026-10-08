/** Structural and test gates — mechanically extracted gate domain. */

import {
  lstatSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
} from 'fs';

import {
  basename,
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
} from 'path';

import {
  resolveRead,
  TEST_RESULTS,
} from '../playtest-layout.mjs';
import { validateJsonSchema } from '../json-schema.mjs';
import { validateObjectivesDocument } from '../objectives-contract.mjs';
import {
  readJSON,
  sha256File,
  listFiles,
} from '../shared/gate-io.mjs';
import {
  gameDir,
  playtestDir,
  fail,
  fileExists,
} from '../gate-context.mjs';
import {
  collectLayoutSourceContract,
  validRuntimeSourceContract,
  canonicalPipelineReference,
  validateInteractionRouting,
  QUALITY_PROFILES,
  qualityProfileName,
  latestTestInputMtime,
  normalizeGenre,
  manifestEntries,
} from './support.mjs';

function gateBasicStructure() {
  const indexPath = join(gameDir, 'index.html');
  const mainPath = join(gameDir, 'src/main.js');
  if (!fileExists(indexPath)) fail('index.html missing');
  if (!fileExists(mainPath)) fail('src/main.js missing');
  if (!fileExists(join(gameDir, 'src/config.js'))) fail('src/config.js missing');
  if (!fileExists(join(gameDir, 'GAME_DESIGN.md'))) fail('GAME_DESIGN.md missing');

  // Check __phaserGame exposure
  const mainJs = fileExists(mainPath) ? readFileSync(mainPath, 'utf-8') : '';
  if (!mainJs.includes('__phaserGame')) fail('main.js does not expose window.__phaserGame');

  // Check Phaser version is v4 (not v3)
  const indexHtml = fileExists(indexPath) ? readFileSync(indexPath, 'utf-8') : '';
  if (indexHtml.includes('phaser@3') || indexHtml.includes('phaser@2')) {
    fail('index.html loads Phaser 3 or 2 — must use Phaser 4 (phaser@4)');
  }
  if (!indexHtml.includes('phaser@4') && !indexHtml.includes('phaser@4.')) {
    fail('index.html does not reference phaser@4 CDN');
  }
}

function gateStateJson(requireIdentity = false) {
  if (fileExists(join(playtestDir, 'state-transaction.pending.json'))) {
    fail('state/resume transaction is incomplete — call write_pipeline_state to recover it');
  }
  const statePath = join(playtestDir, 'state.json');
  if (!fileExists(statePath)) { fail('state.json missing'); return null; }
  const state = readJSON(statePath);
  if (!state) { fail('state.json is invalid JSON'); return null; }
  if (requireIdentity) {
    if (typeof state.pipeline_run_id !== 'string' || !state.pipeline_run_id) {
      fail('state.json missing pipeline_run_id — rewrite state through write_pipeline_state');
    }
    if (typeof state.build_digest !== 'string' || !state.build_digest.startsWith('sha256:')) {
      fail('state.json missing canonical build_digest — rewrite state through write_pipeline_state');
    }
    const resume = readJSON(join(playtestDir, 'resume.json'));
    if (
      !resume
      || resume.pipeline_run_id !== state.pipeline_run_id
      || resume.build_digest !== state.build_digest
      || resume.state_transaction_id !== state.state_transaction_id
      || resume.genre !== (state.genre ?? null)
      || resume.interaction_profile !== (state.interaction_profile ?? null)
      || resume.quality_profile !== (state.quality_profile ?? null)
      || resume.level_model !== (state.level_model ?? null)
    ) {
      fail('state.json and resume.json are not one identity/transaction snapshot');
    }
    const stateLevelFiles = state.level_files ?? null;
    const resumeLevelFiles = resume?.level_files ?? null;
    if (JSON.stringify(stateLevelFiles) !== JSON.stringify(resumeLevelFiles)) {
      fail('state.json and resume.json level_files inventories do not match');
    }
    if (!normalizeGenre(state.genre)) {
      fail(`state.json genre '${state.genre ?? '<missing>'}' is invalid`);
    }
    if (!['arcade-gravity', 'arcade-no-gravity', 'matter', 'none'].includes(state.interaction_profile)) {
      fail(`state.json interaction_profile '${state.interaction_profile ?? '<missing>'}' is invalid`);
    }
    if (
      state.design_axes
      && typeof state.design_axes === 'object'
      && state.design_axes.physics !== undefined
      && state.design_axes.physics !== state.interaction_profile
    ) {
      fail('state.json interaction_profile must equal design_axes.physics');
    }
    const profile = String(state.quality_profile || '').toLowerCase();
    if (!profile) {
      fail('state.json missing quality_profile — rewrite startup state');
    }
    if (!Object.hasOwn(QUALITY_PROFILES, profile)) {
      fail(`state.json quality_profile '${profile}' is unknown`);
    }
    const levelModel = state.level_model;
    if (levelModel === undefined) {
      fail('state.json missing level_model — rewrite startup state');
    } else if (!['authored', 'procedural', 'board', 'none'].includes(levelModel)) {
      fail(`state.json level_model '${levelModel}' is invalid`);
    }
    if (
      levelModel !== undefined
      && state.level_design_required !== (levelModel === 'authored')
    ) {
      fail('state.json level_design_required must equal (level_model === "authored")');
    }
    if (levelModel !== 'authored' && state.has_levels === true) {
      fail('state.json has_levels=true is invalid unless level_model is "authored"');
    }
    if (levelModel !== 'authored' && state.level_files !== undefined) {
      fail('state.json level_files is valid only for level_model="authored"');
    }
    if (
      levelModel === 'authored'
      && state.level_files !== undefined
      && state.has_levels !== true
    ) {
      fail('state.json authored level_files requires has_levels=true');
    }
    if (levelModel === 'authored' && state.has_levels === true) {
      const levelFiles = state.level_files;
      if (!Array.isArray(levelFiles) || levelFiles.length === 0) {
        fail(
          'state.json authored has_levels=true requires a non-empty level_files '
          + 'inventory — revalidate Level Design before resuming',
        );
      } else {
        const levelsDir = resolve(gameDir, 'levels');
        let realLevelsDir = null;
        try {
          if (lstatSync(levelsDir).isSymbolicLink()) {
            fail('state.json authored levels/ directory must not be a symlink');
          } else {
            realLevelsDir = realpathSync(levelsDir);
            if (dirname(realLevelsDir) !== realpathSync(gameDir)) {
              fail('state.json authored levels/ directory escapes GAME_DIR');
              realLevelsDir = null;
            }
          }
        } catch {
          fail('state.json authored levels/ directory is missing or unreadable');
        }
        const seen = new Set();
        const realSeen = new Set();
        for (const [index, value] of levelFiles.entries()) {
          if (
            typeof value !== 'string'
            || !isAbsolute(value)
            || !/^stage_\d+\.json$/.test(basename(value))
            || resolve(dirname(value)) !== levelsDir
          ) {
            fail(
              `state.json level_files[${index}] must be an absolute `
              + `levels/stage_<number>.json path directly under ${levelsDir}`,
            );
            continue;
          }
          const normalized = resolve(value);
          if (seen.has(normalized)) {
            fail(`state.json level_files contains duplicate path '${value}'`);
            continue;
          }
          seen.add(normalized);
          if (!fileExists(normalized)) {
            fail(`state.json level_files entry is missing: ${value}`);
            continue;
          }
          try {
            if (lstatSync(normalized).isSymbolicLink()) {
              fail(`state.json level_files entry must not be a symlink: ${value}`);
              continue;
            }
            if (!statSync(normalized).isFile()) {
              fail(`state.json level_files entry is not a regular file: ${value}`);
              continue;
            }
            const real = realpathSync(normalized);
            if (realLevelsDir === null || dirname(real) !== realLevelsDir) {
              fail(`state.json level_files entry escapes levels/: ${value}`);
              continue;
            }
            if (realSeen.has(real)) {
              fail(`state.json level_files resolves to duplicate file: ${value}`);
              continue;
            }
            realSeen.add(real);
          } catch {
            fail(`state.json level_files entry is unreadable: ${value}`);
          }
        }
        if (fileExists(levelsDir)) {
          try {
            const currentStages = readdirSync(levelsDir)
              .filter((name) => /^stage_\d+\.json$/.test(name))
              .sort(
                (left, right) => (
                  Number(left.match(/\d+/)?.[0]) - Number(right.match(/\d+/)?.[0])
                  || left.localeCompare(right)
                ),
              )
              .map((name) => resolve(levelsDir, name));
            if (
              currentStages.length !== seen.size
              || currentStages.some((path, index) => (
                !seen.has(path) || resolve(levelFiles[index] || '') !== path
              ))
            ) {
              fail(
                'state.json level_files must exactly match the complete current '
                + 'levels/stage_*.json inventory in progression order',
              );
            }
          } catch {
            fail('state.json authored levels/ inventory is unreadable');
          }
        }
      }
    }
  }
  return state;
}

function gateTestsExist() {
  const testsDir = join(playtestDir, 'tests');
  const testFiles = listFiles(
    testsDir,
    (_path, name) => name.endsWith('.js') || name.endsWith('.mjs'),
  );
  if (testFiles.length === 0) fail('No test files found in .playtest/tests/');

  const manifestPath = join(testsDir, 'manifest.json');
  const manifest = readJSON(manifestPath);
  if (!manifest) {
    fail('manifest.json missing or invalid in .playtest/tests/');
    return testFiles.length;
  }
  if (
    manifest.qualityProfile
    && manifest.qualityProfile !== qualityProfileName()
  ) {
    fail('asset-manifest qualityProfile does not match state.json');
  }
  if (!Number.isInteger(manifest.version) || manifest.version < 1) {
    fail('tests/manifest.json must declare integer version >= 1');
  }
  if (!Array.isArray(manifest.tests) || manifest.tests.length === 0) {
    fail('tests/manifest.json must contain a non-empty tests[] array');
    return testFiles.length;
  }

  const listed = new Set();
  for (const entry of manifest.tests) {
    const rel = String(entry?.file || '').replaceAll('\\', '/');
    if (!rel || rel.startsWith('/') || rel.split('/').includes('..')) {
      fail(`tests/manifest.json contains unsafe or missing file path '${rel || '<missing>'}'`);
      continue;
    }
    if (listed.has(rel)) {
      fail(`tests/manifest.json lists '${rel}' more than once`);
      continue;
    }
    listed.add(rel);
    if (!fileExists(join(testsDir, rel))) {
      fail(`tests/manifest.json references missing test '${rel}'`);
    }
    if (!entry.id || !entry.name || !Array.isArray(entry.tags) || !Array.isArray(entry.covers)) {
      fail(`tests/manifest.json entry '${rel}' is missing id/name/tags/covers metadata`);
    }
  }
  for (const path of testFiles) {
    const rel = relative(testsDir, path).replaceAll('\\', '/');
    if (!listed.has(rel)) fail(`test '${rel}' is not declared in tests/manifest.json`);
  }
  return testFiles.length;
}

function gateRubricContract() {
  const rubricPath = join(playtestDir, 'inputs', 'rubric.json');
  const rubric = readJSON(rubricPath);
  if (!rubric) {
    fail('.playtest/inputs/rubric.json missing or invalid — scaffold must project it from the GDD');
    return;
  }
  if (!Number.isInteger(rubric.version) || rubric.version < 1) {
    fail('rubric.json must declare integer version >= 1');
  }
  for (const axis of ['intention', 'visual', 'interaction']) {
    const items = rubric[axis];
    if (
      !Array.isArray(items)
      || items.length === 0
      || items.some((item) => typeof item !== 'string' || !item.trim())
    ) {
      fail(`rubric.json ${axis} must be a non-empty array of checkable statements`);
    }
  }
  const gddPath = join(gameDir, 'GAME_DESIGN.md');
  if (
    fileExists(gddPath)
    && statSync(rubricPath).mtimeMs < statSync(gddPath).mtimeMs
  ) {
    fail('rubric.json is stale (older than GAME_DESIGN.md) — re-project the quality contract');
  }
}
function gateScenesImplemented(state) {
  if (!state) return;
  if (state.scenes?.pending?.length > 0) {
    fail(`Scenes not implemented: ${state.scenes.pending.join(', ')}`);
  }
}
function gateTestsPassing(state) {
  const resultPath = resolveRead(playtestDir, TEST_RESULTS);
  if (!fileExists(resultPath)) {
    fail('formal test-results.json missing — run mcp__gca__run_tests without solo:true');
    return;
  }
  const report = readJSON(resultPath);
  if (!report) {
    fail('formal test-results.json is invalid JSON');
    return;
  }
  const run = report.result;
  const summary = run && typeof run === 'object' ? run.summary : null;
  const canonicalTestsDir = resolve(join(playtestDir, 'tests'));
  if (report.source !== 'test-runner.mjs' || !report.run_id) {
    fail('formal test-results.json provenance missing — rerun through mcp__gca__run_tests');
  }
  if (report.scope?.mode !== 'formal' || report.scope?.full_suite !== true ||
      Object.keys(report.scope?.selectors || {}).length > 0 ||
      resolve(String(report.scope?.tests || '')) !== canonicalTestsDir) {
    fail('canonical test-results.json is not a full unfiltered .playtest/tests/ run');
  }
  if (run?.mode !== 'formal') {
    fail('test-results.json did not come from formal mode — solo/scoped evidence cannot satisfy the release gate');
  }
  if (!summary || !Number.isFinite(Number(summary.total)) || Number(summary.total) <= 0) {
    fail('formal test suite executed zero tests — test-results.json must have summary.total > 0');
  } else if (!Array.isArray(run.tests) || run.tests.length !== Number(summary.total)) {
    fail('formal test-results.json tests[] does not match summary.total');
  } else if (
    run.tests.filter((test) => test?.status === 'PASS').length !== Number(summary.pass || 0) ||
    run.tests.filter((test) => test?.status === 'FAIL').length !== Number(summary.fail || 0)
  ) {
    fail('formal test-results.json status counts do not match summary pass/fail');
  } else if (Number(summary.skip || 0) > 0) {
    fail(`formal full suite skipped ${Number(summary.skip)} test(s)`);
  } else if (Number(summary.fail || 0) > 0 || report.returncode !== 0) {
    fail(`${Number(summary.fail || 0)} formal test(s) failing (runner returncode ${report.returncode ?? 'unknown'})`);
  }
  const inputM = latestTestInputMtime();
  if (inputM && statSync(resultPath).mtimeMs < inputM) {
    fail('formal test-results.json is stale (older than code, assets, levels, contracts, config, or test-suite changes) — re-run the full test suite');
  }

  // State remains a secondary consistency signal, never the source of truth.
  if (state?.tests?.failing > 0) console.warn(`⚠️ state.json still reports ${state.tests.failing} failing test(s); canonical formal results passed.`);
  if (state?.tests?.stale > 0) console.warn(`⚠️ state.json still reports ${state.tests.stale} stale test(s); canonical formal results are fresh.`);
}

function gateNoOpenIssues(state) {
  if (!state) return;
  if (state.known_issues?.length > 0) fail(`${state.known_issues.length} known issues unresolved`);
  if (state.design_debt?.length > 0) fail(`${state.design_debt.length} design debt items unresolved`);
}
function validateProjectedContract(relativePath, schemaName) {
  const contractPath = join(gameDir, relativePath);
  const schemaPath = canonicalPipelineReference('scaffold', 'schemas', schemaName);
  if (!schemaPath) {
    fail(`canonical Scaffold schema '${schemaName}' missing from the repository`);
    return null;
  }
  const schema = readJSON(schemaPath);
  if (!schema || typeof schema.$id !== 'string') {
    fail(`canonical Scaffold schema '${schemaName}' is invalid`);
    return null;
  }
  const contract = readJSON(contractPath);
  if (!contract) {
    fail(`${relativePath} missing or invalid — Scaffold must project it from the GDD`);
    return null;
  }
  const errors = validateJsonSchema(contract, schema, relativePath);
  if (errors.length) {
    fail(
      `${relativePath} violates ${schemaName}: `
      + `${errors.slice(0, 8).join('; ')}`
      + `${errors.length > 8 ? `; +${errors.length - 8} more` : ''}`,
    );
  }
  return contract;
}
function gateProjectedContracts() {
  const manifest = validateProjectedContract(
    'assets/asset-manifest.json',
    'asset-manifest.schema.json',
  );
  const interaction = validateProjectedContract(
    '.playtest/inputs/interaction-contract.json',
    'interaction-contract.schema.json',
  );
  const objectives = validateProjectedContract(
    '.playtest/inputs/objectives.json',
    'objectives.schema.json',
  );
  const layout = validateProjectedContract(
    '.playtest/inputs/layout-contract.json',
    'layout-contract.schema.json',
  );

  for (const error of (objectives ? validateObjectivesDocument(objectives) : [])) {
    fail(`.playtest/inputs/objectives.json runtime contract: ${error}`);
  }

  const sceneKeys = Object.keys(layout?.scenes || {});
  const waived = Array.isArray(layout?.waived_scenes) ? layout.waived_scenes : [];
  if (layout && sceneKeys.length === 0) {
    fail('.playtest/inputs/layout-contract.json must project at least one GDD scene');
  }
  for (const key of waived) {
    if (!sceneKeys.includes(key)) {
      fail(`layout-contract waived_scenes references undeclared scene '${key}'`);
    }
  }
  if (new Set(waived).size !== waived.length) {
    fail('layout-contract waived_scenes contains duplicates');
  }
  if (layout && manifest) {
    const layoutSources = collectLayoutSourceContract(layout);
    for (const error of layoutSources.errors) {
      fail(`layout-contract source violation: ${error}`);
    }
    if (!validRuntimeSourceContract(manifest)) {
      fail('projected asset-manifest runtimeSources must exactly match recursive layout sources');
    }
    const manifestKeys = new Set(
      manifestEntries(manifest).map((entry) => String(entry?.key || '')),
    );
    for (const key of layoutSources.fileKeys.keys()) {
      if (!manifestKeys.has(key)) {
        fail(`projected file-backed layout key '${key}' is missing its asset-manifest row`);
      }
    }
    if (manifest.fileAssetsRequired === false && layoutSources.fileKeys.size > 0) {
      fail(
        'projected no-file asset contract contains file-backed layout obligations: '
        + [...layoutSources.fileKeys.keys()].join(', '),
      );
    }
  }

  if (interaction) {
    validateInteractionRouting(interaction, 'scaffold projection');
    const layers = new Set(interaction.layers || []);
    if (layers.size !== (interaction.layers || []).length) {
      fail('interaction-contract layers contains duplicates');
    }
  }
}

function gateCanonicalHelpers() {
  const helpers = [
    {
      localPath: join(gameDir, 'src', 'display-utils.js'),
      canonicalPath: canonicalPipelineReference(
        'implement',
        'runtime',
        'display-utils.js',
      ),
      required: true,
    },
    {
      localPath: join(gameDir, 'src', 'utils', 'particle-recipes.js'),
      canonicalPath: canonicalPipelineReference('implement', 'particle_recipes.js'),
      required: false,
    },
    {
      localPath: join(gameDir, 'src', 'utils', 'character-controller.js'),
      canonicalPath: canonicalPipelineReference('implement', 'character-controller.js'),
      required: false,
    },
  ];
  for (const { localPath, canonicalPath, required } of helpers) {
    if (!fileExists(localPath)) {
      if (required) fail(`${relative(gameDir, localPath)} is required`);
      continue;
    }
    if (!canonicalPath) {
      fail(`canonical runtime helper missing for ${relative(gameDir, localPath)}`);
      continue;
    }
    if (sha256File(localPath) !== sha256File(canonicalPath)) {
      fail(`${relative(gameDir, localPath)} drifted from its canonical helper`);
    }
  }
}
function gateTestCoverage(state) {
  if (!state) return;
  const implemented = state.scenes?.implemented?.length || 0;
  const formal = readJSON(resolveRead(playtestDir, TEST_RESULTS));
  const formalTotal = Number(formal?.result?.summary?.total);
  const testCount = Number.isFinite(formalTotal) ? formalTotal : (state.tests?.total || 0);
  // Rough heuristic: at least 3 tests per implemented scene
  const minTests = implemented * 3;
  if (testCount < minTests) {
    fail(`Only ${testCount} tests for ${implemented} scenes (minimum ~${minTests})`);
  }
}

export {
  gateBasicStructure,
  gateStateJson,
  gateTestsExist,
  gateRubricContract,
  gateScenesImplemented,
  gateTestsPassing,
  gateNoOpenIssues,
  gateProjectedContracts,
  gateCanonicalHelpers,
  gateTestCoverage,
} ;
