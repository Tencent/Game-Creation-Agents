import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, statSync,
  symlinkSync, writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { deflateSync } from 'node:zlib';
import {
  gddSlotContextSha256,
  mergeRuntimeRegistrySnapshots,
  slotContractSha256,
} from '../../../src/gca_mcp/runners/verify/shared/gate-io.mjs';
import { metadataFor, CHECK_METADATA } from '../../../src/gca_mcp/runners/verify/gate-metadata.mjs';

const gateScript = fileURLToPath(new URL(
  '../../../src/gca_mcp/runners/verify/gate-check.mjs',
  import.meta.url,
));
const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
function writeJson(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2));
}

function readRepoJson(path) {
  return JSON.parse(readFileSync(join(repoRoot, path), 'utf8'));
}

const PNG_CRC_TABLE = Uint32Array.from({ length: 256 }, (_, value) => {
  let crc = value;
  for (let bit = 0; bit < 8; bit += 1) {
    crc = (crc & 1) ? (0xedb88320 ^ (crc >>> 1)) : (crc >>> 1);
  }
  return crc >>> 0;
});

function pngCrc32(parts) {
  let crc = 0xffffffff;
  for (const part of parts) {
    for (const byte of part) {
      crc = PNG_CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data = Buffer.alloc(0)) {
  const typeBytes = Buffer.from(type, 'ascii');
  const chunk = Buffer.alloc(12 + data.length);
  chunk.writeUInt32BE(data.length, 0);
  typeBytes.copy(chunk, 4);
  data.copy(chunk, 8);
  chunk.writeUInt32BE(pngCrc32([typeBytes, data]), 8 + data.length);
  return chunk;
}

function writePngHeader(path, width, height) {
  mkdirSync(dirname(path), { recursive: true });
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 6; // 8-bit RGBA
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  writeFileSync(path, Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(raw)),
    pngChunk('IEND'),
  ]));
}

function writeRgbPng(path, width, height) {
  mkdirSync(dirname(path), { recursive: true });
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 2; // 8-bit RGB, deliberately no alpha
  const stride = width * 3;
  const raw = Buffer.alloc((stride + 1) * height);
  writeFileSync(path, Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(raw)),
    pngChunk('IEND'),
  ]));
}

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

function writeCropEvidence(dir, screen, cropRows, { detect = true } = {}) {
  const sourceRel = `design/${screen}.png`;
  writePngHeader(join(dir, sourceRel), 1280, 720);
  const detectRel = `design/${screen}.detect.json`;
  if (detect) writeJson(join(dir, detectRel), { scene: screen });
  const crops = cropRows.map((row) => {
    const file = row.crop || `${row.label}.png`;
    const path = `design/crops/${screen}/${file}`;
    writePngHeader(join(dir, path), row.width || 100, row.height || 40);
    return {
      label: row.label,
      crop: file,
      path,
      sha256: sha256(join(dir, path)),
      box_px: row.box_px || [0, 0, row.width || 100, row.height || 40],
      source: row.source || 'detect',
      required: row.required,
    };
  });
  const manifestPath = join(dir, `design/crops/${screen}/crops.json`);
  writeJson(manifestPath, {
    source: sourceRel,
    source_sha256: sha256(join(dir, sourceRel)),
    bound_to: detect ? detectRel : null,
    detection_sha256: detect ? sha256(join(dir, detectRel)) : null,
    crops,
    waived: [],
  });
  return manifestPath;
}

function writeBasicGame(dir) {
  writeFileSync(join(dir, 'index.html'), '<script src="phaser@4"></script>\n');
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(join(dir, 'src/main.js'), 'window.__phaserGame = {};\n');
  writeFileSync(join(dir, 'src/config.js'), 'export const CONFIG = {};\n');
  writeFileSync(join(dir, 'GAME_DESIGN.md'), '# game\n');
}

function writeMinimalAuthoredStage(dir) {
  writeJson(join(dir, 'levels/stage_1.json'), {
    level_id: 'stage_1',
    genre: 'puzzle',
    difficulty: 'normal',
    length: 0,
    next_level: null,
    content: {
      kind: 'grid-puzzle',
      data: { width: 4 },
    },
    win_condition: { kind: 'custom', value: 'board_cleared' },
    lose_condition: { kind: 'custom', value: 'moves_exhausted' },
  });
}

function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'gca-gate-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  writeJson(join(dir, 'gca.config.json'), {
    gates: {
      scale: {
        min_entity_px: 1,
        max_entity_px: 4096,
        body_overflow_tolerance: 0.15,
        body_min_ratio: 0.3,
      },
      interaction: {
        enabled: true,
        min_hit_target_px: 24,
        require_for_physics_genres: false,
        require_world_bounds: false,
      },
      tunnel: {
        enabled: true,
        assumed_fps: 60,
        speed_threshold: 200,
        min_body_ratio: 1,
      },
      prefab: { enabled: false },
      subject_margin: {
        enabled: true,
        top: 0.05,
        bottom: 0.05,
        left: 0.05,
        right: 0.05,
      },
      ui_scale: {
        min_px: 8,
        max_texture_px: 4096,
        aspect_tolerance: 0.18,
        require_display_size: true,
        role_px: {
          hud: { max_width: 800, max_height: 800 },
          panel: { max_width: 1000, max_height: 1000 },
        },
        max_canvas_fraction: { hud: 0.55, panel: 0.85, banner: 1.0 },
        layout_check: true,
        overlap_min_px: 12,
      },
    },
  });
  return dir;
}

test('runtime registry snapshots preserve transient textures and animations', () => {
  assert.deepEqual(
    mergeRuntimeRegistrySnapshots([
      {
        loaded_texture_keys: ['initial', 'transient-text'],
        animation_frame_texture_keys: ['idle'],
      },
      {
        loaded_texture_keys: ['action-run'],
        animation_frame_texture_keys: ['run'],
      },
      {
        loaded_texture_keys: ['initial'],
        animation_frame_texture_keys: [],
      },
    ]),
    {
      loaded_texture_keys: ['action-run', 'initial', 'transient-text'],
      animation_frame_texture_keys: ['idle', 'run'],
    },
  );
});

function runGate(gameDir, gate, { step } = {}) {
  const argv = [
    gateScript,
    '--game', gameDir,
    '--gate', gate,
    '--config', join(gameDir, 'gca.config.json'),
  ];
  if (step !== undefined) argv.push('--step', String(step));
  return spawnSync(process.execPath, argv, { encoding: 'utf8' });
}

function configureAssetSanity(dir, policy) {
  const configPath = join(dir, 'gca.config.json');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  config.gates.asset_sanity = policy;
  writeJson(configPath, config);
}

function writeGeneratedAssetFixture(dir, imagePaths, { width, height }) {
  const manifest = readRepoJson(
    '.claude/skills/game-pipeline/references/scaffold/examples/manifest/layout-cluster.json',
  );
  manifest.assets[0].path = imagePaths[0];
  manifest.assets[0].width = width;
  manifest.assets[0].height = height;
  writeCompleteScaffoldBoundary(dir, { manifest });
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  writeJson(statePath, {
    ...state,
    scenes: { pending: [] },
    assets_generated: true,
  });
}

function writeRunIdentity(dir, {
  pipelineRunId = 'pipeline-test',
  verifyRunId = 'verify-test',
  mode = 'iterate',
  status = 'done',
  genre = 'custom',
  interactionProfile = 'none',
  qualityProfile = 'art_directed',
  levelModel = 'none',
  levelFiles = null,
} = {}) {
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  if (!existsSync(manifestPath)) writeJson(manifestPath, { assets: [] });
  const digestResult = runGate(dir, 'build_digest');
  assert.equal(digestResult.status, 0, digestResult.stderr);
  const { build_digest: buildDigest } = JSON.parse(digestResult.stdout);
  const transaction = 'state-transaction-test';
  writeJson(join(dir, '.playtest/state.json'), {
    pipeline_run_id: pipelineRunId,
    build_digest: buildDigest,
    state_transaction_id: transaction,
    genre,
    interaction_profile: interactionProfile,
    quality_profile: qualityProfile,
    level_model: levelModel,
    level_design_required: levelModel === 'authored',
    ...(levelFiles ? { has_levels: true, level_files: levelFiles } : {}),
  });
  writeJson(join(dir, '.playtest/resume.json'), {
    pipeline_run_id: pipelineRunId,
    build_digest: buildDigest,
    state_transaction_id: transaction,
    genre,
    interaction_profile: interactionProfile,
    quality_profile: qualityProfile,
    level_model: levelModel,
    level_files: levelFiles,
  });
  const url = 'http://localhost:8080/';
  const runtimeToken = 'test-runtime-token';
  const runtimeTokenSha256 = createHash('sha256').update(runtimeToken).digest('hex');
  const runtimeTokenUrl = `${url}.playtest/verify/runtime/runtime-token.json?verify_run_id=${verifyRunId}`;
  const tokenPath = join(dir, '.playtest/verify/runtime/runtime-token.json');
  writeJson(tokenPath, {
    schema_version: 1,
    producer: 'gca-mcp.begin_verify_run',
    pipeline_run_id: pipelineRunId,
    build_digest: buildDigest,
    verify_run_id: verifyRunId,
    url,
    runtime_token: runtimeToken,
    runtime_token_sha256: runtimeTokenSha256,
    runtime_token_url: runtimeTokenUrl,
  });
  const runtimeReceiptSha256 = sha256(tokenPath);
  const run = {
    schema_version: 2,
    pipeline_run_id: pipelineRunId,
    build_digest: buildDigest,
    verify_run_id: verifyRunId,
    genre,
    interaction_profile: interactionProfile,
    quality_profile: qualityProfile,
    level_model: levelModel,
    mode,
    status: 'active',
    url,
    runtime_token_receipt: '.playtest/verify/runtime/runtime-token.json',
    runtime_token_url: runtimeTokenUrl,
    runtime_token_sha256: runtimeTokenSha256,
    runtime_receipt_sha256: runtimeReceiptSha256,
  };
  writeJson(join(dir, '.playtest/verify/current-run.json'), run);
  return {
    pipelineRunId,
    verifyRunId,
    buildDigest,
    requestedStatus: status,
    run,
  };
}

function gateFailureMessages(result) {
  assert.equal(result.status, 1, result.stderr);
  const payload = JSON.parse(result.stdout);
  return payload.failures.map((failure) => failure.message);
}

function blockedStderrMessages(result) {
  return result.stderr
    .split('\n')
    .filter((line) => /^\s*BLOCKED:/.test(line))
    .map((line) => line.replace(/^\s*BLOCKED:\s*/, ''));
}

function writeCompleteScaffoldBoundary(dir, {
  manifest,
  layout,
  mode = 'iterate',
  genre = 'puzzle',
  interactionProfile = 'none',
  qualityProfile = 'responsive',
  levelModel = 'board',
} = {}) {
  const root = '.claude/skills/game-pipeline/references/scaffold/examples';
  writeBasicGame(dir);
  writeJson(
    join(dir, 'assets/asset-manifest.json'),
    manifest ?? readRepoJson(`${root}/manifest/layout-cluster.json`),
  );
  writeJson(
    join(dir, '.playtest/inputs/interaction-contract.json'),
    readRepoJson(`${root}/contracts/pointer-interaction.json`),
  );
  writeJson(
    join(dir, '.playtest/inputs/objectives.json'),
    readRepoJson(`${root}/contracts/objectives.json`),
  );
  writeJson(
    join(dir, '.playtest/inputs/layout-contract.json'),
    layout ?? readRepoJson(`${root}/contracts/layout-cluster.json`),
  );
  writeJson(join(dir, '.playtest/inputs/rubric.json'), {
    version: 1,
    intention: ['The core objective is explicit.'],
    visual: ['The projected layout is readable.'],
    interaction: ['The primary pointer action is testable.'],
  });
  const testFile = join(dir, '.playtest/tests/smoke.test.mjs');
  mkdirSync(dirname(testFile), { recursive: true });
  writeFileSync(testFile, 'export default {};\n');
  writeJson(join(dir, '.playtest/tests/manifest.json'), {
    version: 1,
    qualityProfile,
    tests: [{
      id: 'smoke',
      name: 'Smoke',
      file: 'smoke.test.mjs',
      tags: ['smoke'],
      covers: ['start_game'],
    }],
  });
  writeFileSync(
    join(dir, 'src/display-utils.js'),
    readFileSync(
      join(
        repoRoot,
        '.claude/skills/game-pipeline/references/implement/runtime/display-utils.js',
      ),
    ),
  );
  return writeRunIdentity(dir, {
    mode,
    genre,
    interactionProfile,
    qualityProfile,
    levelModel,
  });
}

function writePassingPhaseSevenEvidence(dir, mode) {
  const root = '.claude/skills/game-pipeline/references/scaffold/examples';
  if (mode === 'full-novideo') {
    const configPath = join(dir, 'gca.config.json');
    const config = JSON.parse(readFileSync(configPath, 'utf8'));
    config.verify = { skip_video: true };
    writeJson(configPath, config);
  }
  const manifest = {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: false,
    runtimeSources: ['procedural'],
    assets: [],
  };
  const identity = writeCompleteScaffoldBoundary(dir, {
    manifest,
    layout: readRepoJson(`${root}/contracts/layout-waived-scene.json`),
    mode,
  });
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.scenes = { pending: [] };
  state.assets_generated = true;
  state.visual_verified = true;
  if (mode === 'full-novideo') {
    state.release_waivers = [{
      id: 'video-performance',
      reason: 'Operator accepts missing video performance evidence',
      approved_by: 'operator',
      approved_at: '2026-07-12T00:00:00Z',
      approval_event_id: 'waiver-test-approval',
      pipeline_run_id: identity.pipelineRunId,
      build_digest: identity.buildDigest,
    }];
  }
  writeJson(statePath, state);
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  writeJson(join(dir, '.playtest/verify/assets/asset-integration-report.json'), {
    schema_version: 2,
    producer: 'gca-mcp.write_asset_integration_report',
    pipeline_run_id: identity.pipelineRunId,
    build_digest: identity.buildDigest,
    verify_run_id: identity.verifyRunId,
    manifest_sha256: sha256(manifestPath),
    checks: [],
    blocking_issues: [],
    summary: {
      manifest_entries: 0,
      blocking_issue_count: 0,
    },
  });
  writeVerifyReport(dir, identity);
  return identity;
}

test('step_done follows the renumbered cumulative pipeline phases', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  writeRunIdentity(dir);
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.scenes = { pending: ['GameplayScene'] };
  writeJson(statePath, state);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), 'invalid');
  writeJson(join(dir, '.playtest/verify_middle/slot/orphan.json'), {});

  const results = Object.fromEntries(
    [2, 3, 4, 5, 6, 7].map((step) => [
      step,
      runGate(dir, 'step_done', { step }),
    ]),
  );
  const failures = Object.fromEntries(
    Object.entries(results).map(([step, result]) => {
      assert.equal(result.status, 1, result.stderr);
      return [step, JSON.parse(result.stdout).failures];
    }),
  );
  const find = (step, fragment) => (
    failures[step].find((failure) => failure.message.includes(fragment))
  );
  const has = (step, fragment) => Boolean(find(step, fragment));

  assert.equal(has(2, 'interaction-contract.json missing'), true);
  assert.equal(has(2, 'src/display-utils.js is required'), true);
  assert.equal(has(2, 'No test files found'), true);
  assert.equal(has(2, 'rubric.json missing or invalid'), true);
  assert.equal(has(2, 'Scenes not implemented'), false);
  assert.equal(has(2, 'assets_generated is false'), false);
  assert.equal(has(3, 'Scenes not implemented'), true);
  assert.equal(has(3, 'assets_generated is false'), true);
  assert.equal(has(3, 'asset-integration-report.json missing'), false);
  assert.equal(has(3, 'display-params.json is invalid JSON'), false);
  assert.equal(has(3, "slot-validation 'orphan'"), false);
  assert.equal(has(4, 'asset-integration-report.json missing'), true);
  assert.equal(has(4, 'display-params.json is invalid JSON'), true);
  assert.equal(has(4, "slot-validation 'orphan'"), true);
  assert.deepEqual(failures[5], failures[4]);
  assert.equal(has(5, 'visual_verified is false'), false);
  assert.equal(has(6, 'visual_verified is false'), true);
  assert.equal(has(6, 'no recording.webm found'), false);
  assert.equal(has(7, 'no recording.webm found'), true);
  assert.equal(has(7, 'verify-report.json missing'), true);
  assert.equal(has(7, 'performance-summary.json missing'), true);

  assert.deepEqual(
    find(2, 'No test files found'),
    {
      check: 'test_contract',
      axis: 'tests',
      agent: 'scaffold-agent',
      message: 'No test files found in .playtest/tests/',
    },
  );
  assert.equal(find(3, 'Scenes not implemented').check, 'scenes_implemented');
  assert.equal(find(3, 'Scenes not implemented').agent, 'implement-agent');
  assert.equal(find(3, 'assets_generated is false').check, 'assets_generated');
  assert.equal(find(3, 'assets_generated is false').agent, 'assets-agent');
  assert.equal(find(4, 'asset-integration-report.json missing').check, 'asset_integration');
  assert.equal(
    find(4, 'asset-integration-report.json missing').agent,
    'asset-integration-agent',
  );
  assert.equal(find(4, 'display-params.json is invalid JSON').check, 'entity_scale');
  assert.equal(
    find(4, 'display-params.json is invalid JSON').agent,
    'asset-integration-agent',
  );
  assert.equal(find(4, "slot-validation 'orphan'").check, 'slot_validation');
  assert.equal(find(4, "slot-validation 'orphan'").agent, 'assets-agent');

  for (const [step, result] of Object.entries(results)) {
    assert.deepEqual(
      failures[step].map((failure) => failure.message),
      blockedStderrMessages(result),
    );
  }
});

test('step_done phase 2 accepts the complete Scaffold boundary', (t) => {
  const dir = fixture(t);
  writeCompleteScaffoldBoundary(dir);

  const scaffold = runGate(dir, 'scaffold_done');
  assert.equal(scaffold.status, 0, scaffold.stderr);
  const phase = runGate(dir, 'step_done', { step: 2 });
  assert.equal(phase.status, 0, phase.stderr);
  assert.deepEqual(JSON.parse(phase.stdout), {
    gate: 'step_done',
    blocked: false,
    failures: [],
  });
});

test('step_done phase 2 matches scaffold_done failures', (t) => {
  const dir = fixture(t);

  const scaffold = runGate(dir, 'scaffold_done');
  const phase = runGate(dir, 'step_done', { step: 2 });
  assert.equal(scaffold.status, 1, scaffold.stderr);
  assert.equal(phase.status, 1, phase.stderr);
  assert.deepEqual(
    blockedStderrMessages(phase),
    blockedStderrMessages(scaffold),
  );
});

test('step_done phase 2 matches authored-level failures', (t) => {
  const dir = fixture(t);
  writeCompleteScaffoldBoundary(dir, { levelModel: 'authored' });

  const scaffold = runGate(dir, 'scaffold_done');
  const phase = runGate(dir, 'step_done', { step: 2 });
  assert.equal(scaffold.status, 1, scaffold.stderr);
  assert.equal(phase.status, 1, phase.stderr);
  assert.deepEqual(
    blockedStderrMessages(phase),
    blockedStderrMessages(scaffold),
  );
  assert.ok(
    blockedStderrMessages(phase).some(
      (message) => message.includes('levels/ missing'),
    ),
  );
  const levelFailure = JSON.parse(phase.stdout).failures.find(
    (failure) => failure.message.includes('levels/ missing'),
  );
  assert.equal(levelFailure?.check, 'levels');
  assert.equal(levelFailure?.agent, 'level-design-agent');
  const scaffoldLevelFailure = JSON.parse(scaffold.stdout).failures.find(
    (failure) => failure.message.includes('levels/ missing'),
  );
  assert.equal(scaffoldLevelFailure?.check, 'levels');
  assert.equal(scaffoldLevelFailure?.agent, 'level-design-agent');
});

test('authored Scaffold validates stage data before the runtime loader exists', (t) => {
  const dir = fixture(t);
  writeCompleteScaffoldBoundary(dir, { levelModel: 'authored' });
  writeMinimalAuthoredStage(dir);
  const levelFiles = [join(dir, 'levels/stage_1.json')];
  writeRunIdentity(dir, {
    genre: 'puzzle',
    interactionProfile: 'none',
    qualityProfile: 'responsive',
    levelModel: 'authored',
    levelFiles,
  });

  const scaffold = runGate(dir, 'scaffold_done');
  assert.equal(scaffold.status, 0, scaffold.stderr);

  const phaseTwo = runGate(dir, 'step_done', { step: 2 });
  assert.equal(phaseTwo.status, 0, phaseTwo.stderr);

  const runtimeLevels = runGate(dir, 'levels');
  assert.notEqual(runtimeLevels.status, 0);
  assert.match(runtimeLevels.stderr, /LevelLoader\.js missing/);
  const runtimeFailure = JSON.parse(runtimeLevels.stdout).failures.find(
    (failure) => failure.message.includes('LevelLoader.js missing'),
  );
  assert.equal(runtimeFailure?.check, 'level_runtime');
  assert.equal(runtimeFailure?.axis, 'code');
  assert.equal(runtimeFailure?.agent, 'implement-agent');

  const phaseThree = runGate(dir, 'step_done', { step: 3 });
  assert.notEqual(phaseThree.status, 0);
  assert.match(phaseThree.stderr, /LevelLoader\.js missing/);
  const phaseThreeFailure = JSON.parse(phaseThree.stdout).failures.find(
    (failure) => failure.message.includes('LevelLoader.js missing'),
  );
  assert.equal(phaseThreeFailure?.check, 'level_runtime');
  assert.equal(phaseThreeFailure?.agent, 'implement-agent');
});

test('authored state and resume must carry the same complete level inventory', (t) => {
  const dir = fixture(t);
  writeCompleteScaffoldBoundary(dir, { levelModel: 'authored' });
  writeMinimalAuthoredStage(dir);
  const levelFiles = [join(dir, 'levels/stage_1.json')];
  writeRunIdentity(dir, {
    genre: 'puzzle',
    interactionProfile: 'none',
    qualityProfile: 'responsive',
    levelModel: 'authored',
    levelFiles,
  });

  const resumePath = join(dir, '.playtest/resume.json');
  const resume = JSON.parse(readFileSync(resumePath, 'utf8'));
  writeJson(resumePath, { ...resume, level_files: [] });
  let result = runGate(dir, 'scaffold_done');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /level_files inventories do not match/);

  writeJson(resumePath, { ...resume, level_files: levelFiles });
  result = runGate(dir, 'scaffold_done');
  assert.equal(result.status, 0, result.stderr);

  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  writeJson(statePath, { ...state, has_levels: false });
  result = runGate(dir, 'scaffold_done');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /level_files requires has_levels=true/);
});

test('authored runtime loader cannot embed dispatch-time absolute paths', (t) => {
  const dir = fixture(t);
  writeCompleteScaffoldBoundary(dir, { levelModel: 'authored' });
  writeMinimalAuthoredStage(dir);
  const levelFiles = [join(dir, 'levels/stage_1.json')];
  writeRunIdentity(dir, {
    genre: 'puzzle',
    interactionProfile: 'none',
    qualityProfile: 'responsive',
    levelModel: 'authored',
    levelFiles,
  });
  mkdirSync(join(dir, 'src/level'), { recursive: true });
  writeFileSync(
    join(dir, 'src/level/LevelLoader.js'),
    `export const load = () => fetch('${levelFiles[0]}').then(`
      + '(response) => response.json()).then(({ content }) => content.kind);\n',
  );

  const result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  const failure = JSON.parse(result.stdout).failures.find(
    (entry) => entry.message.includes('absolute GAME_DIR path'),
  );
  assert.equal(failure?.check, 'level_runtime');
  assert.equal(failure?.agent, 'implement-agent');
});

test('step_done phase 4 ignores stale finalized verify run when re-sealing integration', (t) => {
  const dir = fixture(t);
  const root = '.claude/skills/game-pipeline/references/scaffold/examples';
  const manifest = {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: false,
    runtimeSources: ['procedural'],
    assets: [],
  };
  const identity = writeCompleteScaffoldBoundary(dir, {
    manifest,
    layout: readRepoJson(`${root}/contracts/layout-waived-scene.json`),
    mode: 'full',
  });
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.scenes = { pending: [] };
  state.assets_generated = true;
  writeJson(statePath, state);

  const manifestPath = join(dir, 'assets/asset-manifest.json');
  writeJson(join(dir, '.playtest/verify/assets/asset-integration-report.json'), {
    schema_version: 2,
    producer: 'gca-mcp.write_asset_integration_report',
    pipeline_run_id: identity.pipelineRunId,
    build_digest: identity.buildDigest,
    manifest_sha256: sha256(manifestPath),
    checks: [],
    blocking_issues: [],
    summary: {
      manifest_entries: 0,
      blocking_issue_count: 0,
    },
  });
  writeJson(join(dir, '.playtest/verify/current-run.json'), {
    ...identity.run,
    status: 'finalized',
    result_status: 'passed',
    report: '.playtest/verify/verify-report.json',
    report_sha256: 'stale-report-sha256',
  });

  const phase = runGate(dir, 'step_done', { step: 4 });
  assert.equal(phase.status, 0, phase.stderr);
  assert.deepEqual(JSON.parse(phase.stdout), {
    gate: 'step_done',
    blocked: false,
    failures: [],
  });

  const release = runGate(dir, 'ready_to_ship');
  assert.equal(release.status, 1, release.stderr);
  assert.match(
    release.stderr,
    /asset-integration-report\.json is not sealed to the current verify run/,
  );
  const phaseSeven = runGate(dir, 'step_done', { step: 7 });
  assert.equal(phaseSeven.status, 1, phaseSeven.stderr);
  assert.match(
    phaseSeven.stderr,
    /asset-integration-report\.json is not sealed to the current verify run/,
  );
});

test('step_done phase 4 rejects integration receipts not sealed to active verify run', (t) => {
  const dir = fixture(t);
  const root = '.claude/skills/game-pipeline/references/scaffold/examples';
  const manifest = {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: false,
    runtimeSources: ['procedural'],
    assets: [],
  };
  const identity = writeCompleteScaffoldBoundary(dir, {
    manifest,
    layout: readRepoJson(`${root}/contracts/layout-waived-scene.json`),
    mode: 'full',
  });
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.scenes = { pending: [] };
  state.assets_generated = true;
  writeJson(statePath, state);

  const manifestPath = join(dir, 'assets/asset-manifest.json');
  writeJson(join(dir, '.playtest/verify/assets/asset-integration-report.json'), {
    schema_version: 2,
    producer: 'gca-mcp.write_asset_integration_report',
    pipeline_run_id: identity.pipelineRunId,
    build_digest: identity.buildDigest,
    verify_run_id: 'verify-stale',
    manifest_sha256: sha256(manifestPath),
    checks: [],
    blocking_issues: [],
    summary: {
      manifest_entries: 0,
      blocking_issue_count: 0,
    },
  });

  const result = runGate(dir, 'step_done', { step: 4 });
  assert.equal(result.status, 1, result.stderr);
  assert.match(
    result.stderr,
    /asset-integration-report\.json is not sealed to the current verify run/,
  );
});

test('phase 4 ignores a finalized slot seal but strict slot validation rejects it', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  const image = join(dir, 'assets/sprites/player/idle-sheet.png');
  writePngHeader(image, 16, 16);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [spriteManifestEntry('player')],
  });
  writeSlotReceipt(dir, 'player', image);
  const identity = writeRunIdentity(dir, { mode: 'full' });
  writeJson(join(dir, '.playtest/verify/current-run.json'), {
    ...identity.run,
    status: 'finalized',
    result_status: 'passed',
  });

  const phase = runGate(dir, 'step_done', { step: 4 });
  assert.doesNotMatch(
    phase.stderr,
    /slot-validation 'player' is not sealed to the current verify run/,
  );

  const strict = runGate(dir, 'slot_validation');
  assert.equal(strict.status, 1, strict.stderr);
  assert.match(
    strict.stderr,
    /slot-validation 'player' is not sealed to the current verify run/,
  );
  const phaseSeven = runGate(dir, 'step_done', { step: 7 });
  assert.equal(phaseSeven.status, 1, phaseSeven.stderr);
  assert.match(
    phaseSeven.stderr,
    /slot-validation 'player' is not sealed to the current verify run/,
  );
});

test('step_done phase 4 requires sprite integration artifacts', (t) => {
  const dir = fixture(t);
  const root = '.claude/skills/game-pipeline/references/scaffold/examples';
  const manifest = readRepoJson(`${root}/manifest/sprite-per-action.json`);
  const identity = writeCompleteScaffoldBoundary(dir, {
    manifest,
    layout: readRepoJson(`${root}/contracts/layout-cluster.json`),
    qualityProfile: 'art_directed',
  });
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.scenes = { pending: [] };
  state.assets_generated = true;
  writeJson(statePath, state);

  const manifestPath = join(dir, 'assets/asset-manifest.json');
  writeJson(join(dir, '.playtest/verify/assets/asset-integration-report.json'), {
    schema_version: 2,
    producer: 'gca-mcp.write_asset_integration_report',
    pipeline_run_id: identity.pipelineRunId,
    build_digest: identity.buildDigest,
    manifest_sha256: sha256(manifestPath),
    checks: manifest.assets.map((entry) => ({ key: entry.key, wired: true, files: [] })),
    blocking_issues: [],
    summary: {
      manifest_entries: manifest.assets.length,
      blocking_issue_count: 0,
    },
  });

  const result = runGate(dir, 'step_done', { step: 4 });
  assert.equal(result.status, 1, result.stderr);
  const failures = JSON.parse(result.stdout).failures;
  assert.ok(
    failures.some((failure) => failure.message.includes(
      'display-params.json missing for sprite entities',
    )),
  );
  assert.ok(
    failures.some((failure) => failure.message.includes('slot-validation missing')),
  );
  assert.equal(
    failures.find((failure) => failure.message.includes(
      'display-params.json missing for sprite entities',
    ))?.agent,
    'asset-integration-agent',
  );
  assert.equal(
    failures.find((failure) => failure.message.includes('slot-validation missing'))?.agent,
    'assets-agent',
  );

  writeJson(join(dir, '.playtest/inputs/display-params.json'), { entities: {} });
  const emptyLedger = runGate(dir, 'step_done', { step: 4 });
  assert.equal(emptyLedger.status, 1, emptyLedger.stderr);
  assert.match(
    emptyLedger.stderr,
    /display-params entities omit 1 required sprite key\(s\): player/,
  );
});

test('step_done phase 4 requires the measured UI ledger', (t) => {
  const dir = fixture(t);
  const manifest = {
    schema_version: 2,
    qualityProfile: 'minimal',
    fileAssetsRequired: false,
    runtimeSources: ['live-text'],
    assets: [],
  };
  const layout = {
    schema_version: 1,
    canvas: { designWidth: 1280, designHeight: 720 },
    scenes: {
      GameScene: {
        reference: 'waived:concept-generation-unavailable',
        elements: [{
          key: 'score',
          bind: 'score',
          kind: 'text',
          box: [0.03, 0.04, 0.2, 0.08],
          origin: [0, 0],
          role: 'status HUD',
          sourceStrategy: 'live-text',
        }],
      },
    },
    waived_scenes: ['GameScene'],
  };
  const identity = writeCompleteScaffoldBoundary(dir, {
    manifest,
    layout,
    qualityProfile: 'minimal',
  });
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.scenes = { pending: [] };
  state.assets_generated = true;
  writeJson(statePath, state);

  const manifestPath = join(dir, 'assets/asset-manifest.json');
  writeJson(join(dir, '.playtest/verify/assets/asset-integration-report.json'), {
    schema_version: 2,
    producer: 'gca-mcp.write_asset_integration_report',
    pipeline_run_id: identity.pipelineRunId,
    build_digest: identity.buildDigest,
    verify_run_id: identity.verifyRunId,
    manifest_sha256: sha256(manifestPath),
    checks: [],
    blocking_issues: [],
    summary: {
      manifest_entries: 0,
      blocking_issue_count: 0,
    },
  });

  const result = runGate(dir, 'step_done', { step: 4 });
  assert.equal(result.status, 1, result.stderr);
  const failure = JSON.parse(result.stdout).failures.find(
    (entry) => entry.check === 'ui_proportion',
  );
  assert.equal(failure?.agent, 'asset-integration-agent');
  assert.match(
    failure?.message || '',
    /display-params\.json is missing although 1 UI element\(s\)/,
  );

  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    ui: [{ key: 'score' }, { key: 'score' }],
  });
  const duplicateLedger = runGate(dir, 'step_done', { step: 4 });
  assert.equal(duplicateLedger.status, 1, duplicateLedger.stderr);
  assert.match(
    duplicateLedger.stderr,
    /display-params\.ui contains duplicate measured row\(s\): score/,
  );
});

test('step_done phase 7 accepts approved full-novideo evidence', (t) => {
  const dir = fixture(t);
  writePassingPhaseSevenEvidence(dir, 'full-novideo');

  const phase = runGate(dir, 'step_done', { step: 7 });
  assert.equal(phase.status, 0, phase.stderr);
  assert.deepEqual(JSON.parse(phase.stdout), {
    gate: 'step_done',
    blocked: false,
    failures: [],
  });
});

test('step_done phase 7 accepts full video evidence', (t) => {
  const dir = fixture(t);
  writePassingPhaseSevenEvidence(dir, 'full');

  const phase = runGate(dir, 'step_done', { step: 7 });
  assert.equal(phase.status, 0, phase.stderr);
  assert.deepEqual(JSON.parse(phase.stdout), {
    gate: 'step_done',
    blocked: false,
    failures: [],
  });
});

test('step_done routes pipeline-state failures to the orchestrator', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);

  const result = runGate(dir, 'step_done', { step: 2 });
  assert.equal(result.status, 1, result.stderr);
  const failure = JSON.parse(result.stdout).failures.find(
    (entry) => entry.message === 'state.json missing',
  );
  assert.deepEqual(failure, {
    check: 'pipeline_state',
    axis: 'state',
    agent: 'orchestrator',
    message: 'state.json missing',
  });
});

test('step_done rejects values outside the current phase range', (t) => {
  const dir = fixture(t);
  for (const step of [1, 8, 'three']) {
    const result = runGate(dir, 'step_done', { step });
    const payload = JSON.parse(result.stdout);
    assert.deepEqual(gateFailureMessages(result), [
      'step_done requires an integer --step from 2 through 7 under the current pipeline numbering',
    ]);
    assert.deepEqual(payload.failures[0], {
      check: 'step_done',
      axis: 'state',
      agent: 'orchestrator',
      message: 'step_done requires an integer --step from 2 through 7 under the current pipeline numbering',
    });
  }
});

function runtimeIdentity(run) {
  return {
    schema_version: 1,
    source: 'runtime-identity.mjs',
    verified: true,
    epoch_url: run.url,
    runtime_token_url: run.runtime_token_url,
    runtime_token_sha256: run.runtime_token_sha256,
    runtime_receipt_sha256: run.runtime_receipt_sha256,
    pipeline_run_id: run.pipeline_run_id,
    build_digest: run.build_digest,
    verify_run_id: run.verify_run_id,
    page_url: run.url,
    fetched_url: run.runtime_token_url,
  };
}

function artifactEntry(dir, rel) {
  const path = join(dir, rel);
  return {
    path: rel,
    size_bytes: statSync(path).size,
    sha256: sha256(path),
  };
}

function writeRuntimeRegistry(dir, run) {
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  const loaded = [];
  const animated = [];
  const displayed = [];
  const manifestAssets = (manifest.assets || []).map((entry) => {
    const declared = new Set([entry.key]);
    if (entry.textureKey) declared.add(entry.textureKey);
    for (const action of Object.values(entry.actions || {})) {
      if (action.textureKey) declared.add(action.textureKey);
    }
    const declaredKeys = [...declared].sort();
    return {
      key: entry.key,
      declared_texture_keys: declaredKeys,
      loaded_texture_keys: declaredKeys,
      animation_frame_texture_keys: declaredKeys,
      display_list_texture_keys: [],
      loaded: true,
      used: true,
    };
  });
  for (const item of manifestAssets) {
    loaded.push(...item.loaded_texture_keys);
    animated.push(...item.animation_frame_texture_keys);
  }
  const registry = {
    schema_version: 1,
    producer: 'gca-mcp.capture_runtime_assets',
    pipeline_run_id: run.pipeline_run_id,
    build_digest: run.build_digest,
    verify_run_id: run.verify_run_id,
    url: run.url,
    runtime_token_sha256: run.runtime_token_sha256,
    runtime_receipt_sha256: run.runtime_receipt_sha256,
    runtime_identity: runtimeIdentity(run),
    manifest: 'assets/asset-manifest.json',
    manifest_sha256: sha256(manifestPath),
    loaded_texture_keys: [...new Set(loaded)].sort(),
    animation_frame_texture_keys: [...new Set(animated)].sort(),
    display_list_texture_keys: displayed,
    display_object_texture_bindings: [],
    game_config: { pixelArt: false },
    active_scene_keys: ['GameScene'],
    registered_scene_keys: ['GameScene'],
    reachable_scene_keys: ['GameScene'],
    scenes: [{
      scene: 'GameScene',
      reachable: true,
      active: true,
      visible: true,
      texture_keys: [],
      texture_bindings: [],
    }],
    manifest_assets: manifestAssets,
    summary: {
      loaded_texture_count: new Set(loaded).size,
      animation_texture_count: new Set(animated).size,
      display_texture_count: 0,
      display_object_texture_binding_count: 0,
      runtime_uuid_texture_count: 0,
      registered_scene_count: 1,
      reachable_scene_count: 1,
      manifest_asset_count: manifestAssets.length,
      manifest_loaded_count: manifestAssets.length,
      manifest_used_count: manifestAssets.length,
    },
  };
  const rel = '.playtest/verify/runtime/runtime-assets.json';
  writeJson(join(dir, rel), registry);
  return { rel, registry };
}

function writeVideoChain(dir, run, scenario = 'core-loop') {
  const baseRel = `.playtest/verify/recording/${scenario}`;
  const videoRel = `${baseRel}/recording.webm`;
  const timelineRel = `${baseRel}/state-timeline.json`;
  const performanceRel = `${baseRel}/performance-summary.json`;
  const receiptRel = `${baseRel}/video-receipt.json`;
  const analysisRel = `${baseRel}/video-analysis.json`;
  mkdirSync(join(dir, baseRel), { recursive: true });
  writeFileSync(join(dir, videoRel), 'test video');
  writeJson(join(dir, timelineRel), [{ t: 0, scenes: {} }]);
  writeJson(join(dir, performanceRel), {
    generated_at: '2026-07-13T00:00:00Z',
    runtime_identity: runtimeIdentity(run),
    fps: { p50: 60, min: 55 },
    timings: { boot_seconds: 1 },
    long_tasks: { count: 0, total_duration_ms: 0 },
    memory: { supported: false },
    objects: { growth_count: 0, slope_count_per_second: 0 },
  });
  const receipt = {
    schema_version: 1,
    producer: 'gca-mcp.record_with_state',
    pipeline_run_id: run.pipeline_run_id,
    build_digest: run.build_digest,
    verify_run_id: run.verify_run_id,
    url: run.url,
    runtime_token_sha256: run.runtime_token_sha256,
    runtime_receipt_sha256: run.runtime_receipt_sha256,
    runtime_identity: runtimeIdentity(run),
    video_path: videoRel,
    video_sha256: sha256(join(dir, videoRel)),
    timeline_path: timelineRel,
    timeline_sha256: sha256(join(dir, timelineRel)),
    performance_path: performanceRel,
    performance_sha256: sha256(join(dir, performanceRel)),
  };
  writeJson(join(dir, receiptRel), receipt);
  const analysis = {
    schema_version: 1,
    producer: 'gca-mcp.analyze_video',
    analysis_status: 'completed',
    analysis_errors: [],
    analysis: 'No blocking issues observed.',
    segment_analyses: [{
      segment: 1,
      analysis: 'No blocking issues observed.',
    }],
    pipeline_run_id: run.pipeline_run_id,
    build_digest: run.build_digest,
    verify_run_id: run.verify_run_id,
    url: run.url,
    runtime_token_sha256: run.runtime_token_sha256,
    runtime_receipt_sha256: run.runtime_receipt_sha256,
    runtime_identity: runtimeIdentity(run),
    video_receipt: receiptRel,
    video_receipt_sha256: sha256(join(dir, receiptRel)),
    video_path: videoRel,
    video_sha256: receipt.video_sha256,
    timeline_path: timelineRel,
    timeline_sha256: receipt.timeline_sha256,
  };
  writeJson(join(dir, analysisRel), analysis);
  return {
    receipt,
    analysis,
    artifacts: {
      video_receipts: [artifactEntry(dir, receiptRel)],
      recordings: [artifactEntry(dir, videoRel)],
      state_timelines: [artifactEntry(dir, timelineRel)],
      performance_summaries: [artifactEntry(dir, performanceRel)],
      video_analyses: [artifactEntry(dir, analysisRel)],
    },
  };
}

function finalizeVerifyReport(dir, payload) {
  const runPath = join(dir, '.playtest/verify/current-run.json');
  const run = JSON.parse(readFileSync(runPath, 'utf8'));
  const tokenRel = '.playtest/verify/runtime/runtime-token.json';
  const { rel: registryRel, registry } = writeRuntimeRegistry(dir, run);
  const config = JSON.parse(readFileSync(join(dir, 'gca.config.json'), 'utf8'));
  const video = payload.mode === 'full' && config.verify?.skip_video !== true
    ? writeVideoChain(dir, run)
    : null;
  const report = {
    ...payload,
    schema_version: 4,
    url: run.url,
    runtime_token_sha256: run.runtime_token_sha256,
    runtime_receipt_sha256: run.runtime_receipt_sha256,
    runtime_asset_registry: registry,
    ...(video ? {
      video_receipts: [video.receipt],
      video_analysis: video.analysis,
    } : {}),
    artifacts: {
      ...(payload.artifacts || {}),
      runtime_token_receipt: artifactEntry(dir, tokenRel),
      runtime_asset_registry: artifactEntry(dir, registryRel),
      ...(video?.artifacts || {}),
    },
  };
  const reportPath = join(dir, '.playtest/verify/verify-report.json');
  writeJson(reportPath, report);
  writeJson(runPath, {
    ...run,
    status: 'finalized',
    result_status: report.status,
    report: '.playtest/verify/verify-report.json',
    report_sha256: sha256(reportPath),
  });
  return report;
}

function writeVerifyReport(dir, identity, overrides = {}) {
  return finalizeVerifyReport(dir, {
    schema_version: 3,
    run_id: identity.verifyRunId,
    pipeline_run_id: identity.pipelineRunId,
    build_digest: identity.buildDigest,
    verify_run_id: identity.verifyRunId,
    genre: identity.run.genre,
    interaction_profile: identity.run.interaction_profile,
    quality_profile: identity.run.quality_profile,
    level_model: identity.run.level_model,
    mode: identity.run.mode,
    status: identity.requestedStatus,
    summary: { missing_artifacts: [] },
    issues: [],
    ...overrides,
  });
}

function resealVerifyReport(dir, report) {
  const reportPath = join(dir, '.playtest/verify/verify-report.json');
  const runPath = join(dir, '.playtest/verify/current-run.json');
  writeJson(reportPath, report);
  const run = JSON.parse(readFileSync(runPath, 'utf8'));
  run.report_sha256 = sha256(reportPath);
  writeJson(runPath, run);
}

function spriteManifestEntry(key, actions = ['idle']) {
  const actionMap = Object.fromEntries(actions.map((action) => [action, {
    textureKey: `${key}-${action}`,
    path: `assets/sprites/${key}/${action}-sheet.png`,
    metadata: `assets/sprites/${key}/${action}-sheet.json`,
  }]));
  return {
    key,
    category: 'sprite',
    sourceStrategy: 'generated-image',
    description: `${key} design`,
    layout: 'per-action',
    defaultAction: actions[0],
    path: actionMap[actions[0]].path,
    metadata: actionMap[actions[0]].metadata,
    actions: actionMap,
  };
}

function writeSlotReceipt(dir, key, image, identity = {}) {
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  const gddPath = join(dir, 'GAME_DESIGN.md');
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  const entry = manifest.assets.find((item) => item.key === key);
  writeJson(join(dir, `.playtest/verify_middle/slot/${key}.json`), {
    schema_version: 3,
    producer: 'gca-mcp.validate_asset_slot',
    key,
    action: entry.defaultAction ?? null,
    image,
    image_sha256: sha256(image),
    gdd_sha256: sha256(gddPath),
    manifest_sha256: sha256(manifestPath),
    gdd_slot_context_sha256: gddSlotContextSha256(
      readFileSync(gddPath, 'utf8'),
      key,
      entry.description,
      entry.defaultAction ?? null,
    ),
    slot_contract_sha256: slotContractSha256(entry),
    slot_description_sha256: createHash('sha256')
      .update(entry.description)
      .digest('hex'),
    passed: true,
    verdict: 'PASS',
    ...identity,
  });
}

function measuredEntity(overrides = {}) {
  return {
    scale: 1,
    frameWidth: 100,
    frameHeight: 100,
    contentBbox: { x: 0, y: 0, w: 100, h: 100 },
    bodySpace: 'frame',
    bodyWidth: 100,
    bodyHeight: 100,
    bodyOffsetX: 0,
    bodyOffsetY: 0,
    ...overrides,
  };
}

function writeInteractionContract(dir, contract, {
  genre = 'custom',
  interactionProfile = 'none',
} = {}) {
  writeJson(join(dir, '.playtest/state.json'), {
    genre,
    interaction_profile: interactionProfile,
  });
  writeJson(join(dir, '.playtest/inputs/interaction-contract.json'), {
    genre,
    interaction_profile: interactionProfile,
    ...contract,
  });
}

function formalTestResult({
  total = 1,
  pass = total,
  fail = 0,
  skip = 0,
  mode = 'formal',
  fullSuite = true,
  selectors = {},
  testsPath = '',
  returncode = fail > 0 ? 1 : 0,
} = {}) {
  return {
    source: 'test-runner.mjs',
    run_id: `tests-${Date.now()}-${Math.random()}`,
    returncode,
    scope: { mode, full_suite: fullSuite, selectors, tests: testsPath },
    result: {
      mode,
      tests: Array.from({ length: total }, (_, i) => ({
        name: `test-${i + 1}`,
        status: i < fail ? 'FAIL' : 'PASS',
      })),
      summary: { total, pass, fail, skip },
    },
  };
}

function playHistoryEntry(overrides = {}) {
  return {
    ts: '2026-07-12T00:00:00.000Z',
    scores: { objective: 1 },
    weighted_accuracy: 1,
    build_health: true,
    coverage: { scene_pct: 1, action_pct: 1, region_pct: 1 },
    ...overrides,
  };
}

test('test contract requires a complete producer-owned manifest', (t) => {
  const dir = fixture(t);
  const testsDir = join(dir, '.playtest/tests');
  mkdirSync(join(testsDir, 'smoke'), { recursive: true });
  writeFileSync(join(testsDir, 'smoke/001_boot.js'), 'export default {};\n');

  let result = runGate(dir, 'test_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /manifest\.json missing or invalid/);

  writeJson(join(testsDir, 'manifest.json'), {
    version: 1,
    generated: '2026-07-12T00:00:00Z',
    tests: [{
      id: '001',
      file: 'smoke/001_boot.js',
      name: 'boots',
      tags: ['smoke'],
      covers: ['index.html'],
    }],
  });
  result = runGate(dir, 'test_contract');
  assert.equal(result.status, 0, result.stderr + result.stdout);

  writeFileSync(join(testsDir, 'smoke/002_scene.js'), 'export default {};\n');
  result = runGate(dir, 'test_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /002_scene\.js.*not declared/);
});

test('rubric contract has one declared producer shape', (t) => {
  const dir = fixture(t);
  writeFileSync(join(dir, 'GAME_DESIGN.md'), '# game\n');
  writeJson(join(dir, '.playtest/inputs/rubric.json'), {
    version: 1,
    generated_from: 'GAME_DESIGN.md',
    intention: ['Game boots'],
    visual: ['Critical UI is readable'],
    interaction: ['Primary action changes state'],
  });
  let result = runGate(dir, 'rubric_contract');
  assert.equal(result.status, 0, result.stderr + result.stdout);

  writeJson(join(dir, '.playtest/inputs/rubric.json'), {
    version: 1,
    generated_from: 'GAME_DESIGN.md',
    intention: [],
    visual: ['Critical UI is readable'],
    interaction: ['Primary action changes state'],
  });
  result = runGate(dir, 'rubric_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /intention must be a non-empty array/);
});

test('level gate follows level_model instead of directory or legacy booleans', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'levels'), { recursive: true });
  writeJson(join(dir, '.playtest/state.json'), {
    level_model: 'procedural',
    level_design_required: true,
    has_levels: true,
  });
  let result = runGate(dir, 'levels');
  assert.equal(result.status, 0, result.stderr + result.stdout);

  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'platform',
    level_model: 'authored',
    level_design_required: false,
    has_levels: false,
  });
  result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /No levels\/stage_\*\.json files found/);

  writeJson(join(dir, 'levels/stage_1.json'), {
    level_id: 'stage_1',
    genre: 'platform',
    difficulty: 'normal',
    length: 0,
    next_level: null,
    terrain: [{ chunk: 'flat_ground', x: 0, width: 800, height: 40 }],
    spawns: [],
    win_condition: { kind: 'survive_time', value: 1 },
    lose_condition: { kind: 'lives_zero' },
  });
  mkdirSync(join(dir, 'src/level'), { recursive: true });
  writeFileSync(
    join(dir, 'src/level/LevelLoader.js'),
    'export class LevelLoader { load(stage) { return stage.content.kind; } }\n',
  );
  result = runGate(dir, 'levels');
  assert.equal(result.status, 0, result.stderr + result.stdout);
});

test('level gate validates stages against the canonical schema', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'levels'), { recursive: true });
  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'platform',
    level_model: 'authored',
  });
  writeJson(join(dir, 'levels/stage_1.json'), {
    level_id: 'stage_1',
    genre: 'platform',
    difficulty: 5,
    spawn_trigger: 'time_s',
    spawns: [{ id: 'spawn_1', at: 1, count: 1, interval: 0 }],
  });
  mkdirSync(join(dir, 'src/level'), { recursive: true });
  writeFileSync(join(dir, 'src/level/LevelLoader.js'), 'export class LevelLoader {}\n');

  const result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /violates canonical level_schema\.json/);
  assert.match(result.stderr + result.stdout, /difficulty|spawn_trigger|length/);
});

test('level gate enforces geometry and tower-defense schema constraints', (t) => {
  const dir = fixture(t);
  const statePath = join(dir, '.playtest/state.json');
  const stagePath = join(dir, 'levels/stage_1.json');
  writeJson(statePath, {
    genre: 'platform',
    level_model: 'authored',
  });
  const platformStage = {
    level_id: 'stage_1',
    genre: 'platform',
    difficulty: 'normal',
    length: 128,
    next_level: null,
    terrain: [{ chunk: 'flat_ground', x: 0, width: 128 }],
    win_condition: { kind: 'reach_end' },
    lose_condition: { kind: 'lives_zero' },
  };
  writeJson(stagePath, platformStage);
  mkdirSync(join(dir, 'src/level'), { recursive: true });
  writeFileSync(join(dir, 'src/level/LevelLoader.js'), 'export class LevelLoader {}\n');

  let result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /terrain\[0\]\.height: required/);

  platformStage.terrain[0].height = 32;
  platformStage.terrain[0].width = 0;
  writeJson(stagePath, platformStage);
  result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /width: 0 <= exclusiveMinimum 0/);

  platformStage.terrain[0].width = 128;
  writeJson(stagePath, platformStage);
  result = runGate(dir, 'levels');
  assert.equal(result.status, 0, result.stderr + result.stdout);

  writeJson(statePath, {
    genre: 'tower_defence',
    level_model: 'authored',
  });
  const mapPath = join(dir, 'levels/map-layout.json');
  const mapLayout = {
    canvas: { width: 800, height: 600 },
    core: { x: 700, y: 300 },
    spawns: [{ id: 'entry', x: 0, y: 300 }],
    paths: [{
      id: 'main',
      spawn: 'entry',
      waypoints: [{ x: 0, y: 300 }, { x: 700, y: 300 }],
    }],
    build_slots: [],
  };
  writeJson(mapPath, mapLayout);
  const towerDefenseStage = {
    level_id: 'stage_1',
    genre: 'tower_defence',
    difficulty: 'normal',
    length: 0,
    next_level: null,
    map_layout_ref: 'levels/map-layout.json',
    spawns: [{ type: 'scout', path: 'main', wave: 1 }],
    win_condition: { kind: 'custom', value: 'all_waves_cleared' },
    lose_condition: { kind: 'objective_lost', value: 'core' },
  };
  writeJson(stagePath, towerDefenseStage);

  result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /build_slots: needs at least 1 item/);

  mapLayout.build_slots = [{ x: 350, y: 100 }];
  writeJson(mapPath, mapLayout);
  delete towerDefenseStage.spawns[0].path;
  delete towerDefenseStage.spawns[0].wave;
  writeJson(stagePath, towerDefenseStage);
  result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /spawns\[0\]\.path: required/);
  assert.match(result.stderr + result.stdout, /spawns\[0\]\.wave: required/);

  Object.assign(towerDefenseStage.spawns[0], { path: 'main', wave: 1 });
  writeJson(stagePath, towerDefenseStage);
  result = runGate(dir, 'levels');
  assert.equal(result.status, 0, result.stderr + result.stdout);
});

test('authored level schema supports every Startup genre via content envelope', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'puzzle',
    level_model: 'authored',
    level_design_required: true,
  });
  writeJson(join(dir, 'levels/stage_1.json'), {
    level_id: 'stage_1',
    genre: 'puzzle',
    difficulty: 'normal',
    length: 0,
    next_level: null,
    content: {
      kind: 'grid-puzzle',
      data: { width: 4, height: 4, target: 'clear' },
    },
    win_condition: { kind: 'custom', value: 'board_cleared' },
    lose_condition: { kind: 'custom', value: 'moves_exhausted' },
  });
  mkdirSync(join(dir, 'src/level'), { recursive: true });
  writeFileSync(
    join(dir, 'src/level/LevelLoader.js'),
    'export class LevelLoader { load(stage) { return stage.content.kind; } }\n',
  );
  let result = runGate(dir, 'levels');
  assert.equal(result.status, 0, result.stderr + result.stdout);
  const stagePath = join(dir, 'levels/stage_1.json');
  const stage = JSON.parse(readFileSync(stagePath, 'utf8'));
  stage.genre = 'card';
  writeJson(stagePath, stage);
  result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must exactly equal state\.json genre/);

  stage.genre = 'puzzle';
  stage.content.data = {};
  writeJson(stagePath, stage);
  result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /content must be.*non-empty object/);

  stage.content.data = { width: 4 };
  writeJson(stagePath, stage);
  writeFileSync(
    join(dir, 'src/level/LevelLoader.js'),
    'export class LevelLoader { load(stage) { return stage.level_id; } }\n',
  );
  result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not consume the authored content\.kind/);
});

test('every authored stage requires a consumable payload', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'platform',
    level_model: 'authored',
  });
  writeJson(join(dir, 'levels/stage_1.json'), {
    level_id: 'stage_1',
    genre: 'platform',
    difficulty: 'normal',
    length: 0,
    next_level: null,
    win_condition: { kind: 'survive_time', value: 1 },
    lose_condition: { kind: 'lives_zero' },
  });
  mkdirSync(join(dir, 'src/level'), { recursive: true });
  writeFileSync(join(dir, 'src/level/LevelLoader.js'), 'export class LevelLoader {}\n');
  const result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /authored stage is an empty shell/);
});

test('level gate rejects unresolved and prematurely unlocked paths', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'levels'), { recursive: true });
  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'tower_defence',
    level_model: 'authored',
  });
  writeJson(join(dir, 'levels/map-layout.json'), {
    canvas: { width: 800, height: 600 },
    core: { x: 700, y: 300 },
    spawns: [{ id: 'entry', x: 0, y: 300 }],
    paths: [{
      id: 'late_route',
      spawn: 'entry',
      unlocked_at_wave: 3,
      waypoints: [{ x: 0, y: 300 }, { x: 700, y: 300 }],
    }],
    build_slots: [{ x: 350, y: 100 }],
  });
  const stagePath = join(dir, 'levels/stage_1.json');
  writeJson(stagePath, {
    level_id: 'stage_1',
    genre: 'tower_defence',
    difficulty: 'normal',
    length: 0,
    next_level: null,
    map_layout_ref: 'levels/map-layout.json',
    spawn_trigger: 'time',
    spawns: [{
      id: 'wave_2',
      at: 1,
      type: 'scout',
      path: 'late_route',
      wave: 2,
    }],
    win_condition: { kind: 'custom', value: 'all_waves_cleared' },
    lose_condition: { kind: 'objective_lost', value: 'core' },
  });
  mkdirSync(join(dir, 'src/level'), { recursive: true });
  writeFileSync(join(dir, 'src/level/LevelLoader.js'), 'export class LevelLoader {}\n');

  let result = runGate(dir, 'levels');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr + result.stdout, /before unlocked_at_wave 3/);

  const stage = JSON.parse(readFileSync(stagePath, 'utf8'));
  stage.spawns[0].wave = 3;
  writeJson(stagePath, stage);
  result = runGate(dir, 'levels');
  assert.equal(result.status, 0, result.stderr + result.stdout);
});

// ── Lane-defence build slots (on_path) ─────────────────────────────────
// The default TD shape is towers beside a road. A lane-defence game (plants on
// a lawn row) puts defenders on the route, which can never clear it, so such a
// slot declares on_path:true and the gate inverts the geometry requirement
// instead of dropping it.
function laneDefenceFixture(t, { slots, allowOnPath }) {
  const dir = fixture(t);
  mkdirSync(join(dir, 'levels'), { recursive: true });

  // Isolate map_layout: the td_contract bundle also runs the baked-path,
  // path-alignment and HUD gates, which need artifacts irrelevant here.
  const configPath = join(dir, 'gca.config.json');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  const mapLayout = { enabled: true };
  if (allowOnPath !== undefined) {
    mapLayout.allow_on_path_build_slots = allowOnPath;
  }
  config.gates.tower_defence = {
    map_layout: mapLayout,
    no_baked_path: { enabled: false },
    path_alignment: { enabled: false },
    hud_binding: { enabled: false },
  };
  writeJson(configPath, config);

  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'tower_defence',
    level_model: 'authored',
  });
  // One straight lane across the middle of the canvas.
  writeJson(join(dir, 'levels/map-layout.json'), {
    canvas: { width: 800, height: 600 },
    core: { x: 700, y: 300 },
    road_width: 80,
    spawns: [{ id: 'entry', x: 0, y: 300 }],
    paths: [{
      id: 'lane',
      spawn: 'entry',
      waypoints: [{ x: 0, y: 300 }, { x: 700, y: 300 }],
    }],
    build_slots: slots,
  });
  writeJson(join(dir, 'levels/stage_1.json'), {
    level_id: 'stage_1',
    genre: 'tower_defence',
    difficulty: 'normal',
    length: 0,
    next_level: null,
    map_layout_ref: 'levels/map-layout.json',
    spawn_trigger: 'time',
    spawns: [{ id: 'wave_1', at: 1, type: 'scout', path: 'lane', wave: 1 }],
    win_condition: { kind: 'custom', value: 'all_waves_cleared' },
    lose_condition: { kind: 'objective_lost', value: 'core' },
  });
  mkdirSync(join(dir, 'src/level'), { recursive: true });
  writeFileSync(join(dir, 'src/level/LevelLoader.js'), 'export class LevelLoader {}\n');
  return dir;
}

test('build slot on the lane passes only when it declares on_path', (t) => {
  // Slot sits exactly on the lane centre: distance 0, clearance required 104.
  const onLane = { x: 350, y: 300, width: 64, height: 64 };

  const strict = laneDefenceFixture(t, { slots: [onLane] });
  const blocked = runGate(strict, 'td_contract');
  assert.notEqual(blocked.status, 0);
  assert.match(
    blocked.stderr + blocked.stdout,
    /sits on\/too close to a road[\s\S]*set on_path:true/,
  );

  const declared = laneDefenceFixture(t, {
    slots: [{ ...onLane, on_path: true }],
  });
  const passed = runGate(declared, 'td_contract');
  assert.equal(passed.status, 0, passed.stderr + passed.stdout);
});

test('on_path cannot smuggle an off-road slot past the clearance check', (t) => {
  // Far from the lane (distance 200 > clearance 104), so the inverted
  // requirement must reject it rather than silently exempting it.
  const dir = laneDefenceFixture(t, {
    slots: [{ x: 350, y: 100, width: 64, height: 64, on_path: true }],
  });
  const result = runGate(dir, 'td_contract');
  assert.notEqual(result.status, 0);
  assert.match(
    result.stderr + result.stdout,
    /declares on_path but sits clear of every path/,
  );
});

test('disabling allow_on_path_build_slots restores the strict contract', (t) => {
  const dir = laneDefenceFixture(t, {
    slots: [{ x: 350, y: 300, width: 64, height: 64, on_path: true }],
    allowOnPath: false,
  });
  const result = runGate(dir, 'td_contract');
  assert.notEqual(result.status, 0);
  assert.match(
    result.stderr + result.stdout,
    /sets on_path but[\s\S]*allow_on_path_build_slots is disabled/,
  );
});

test('off-road build slot still passes without on_path', (t) => {
  // Distance 200 from the lane, well beyond the 104px clearance.
  const dir = laneDefenceFixture(t, {
    slots: [{ x: 350, y: 100, width: 64, height: 64 }],
  });
  const result = runGate(dir, 'td_contract');
  assert.equal(result.status, 0, result.stderr + result.stdout);
});

test('routing genre is never inferred outside state.json', (t) => {
  const dir = fixture(t);
  const configPath = join(dir, 'gca.config.json');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  config.gates.tower_defence = {
    map_layout: { enabled: true },
    no_baked_path: { enabled: false },
    path_alignment: { enabled: false },
    hud_binding: { enabled: false },
  };
  writeJson(configPath, config);
  writeFileSync(join(dir, 'GAME_DESIGN.md'), '# Game\n\nGenre: tower_defence\n');
  writeJson(join(dir, 'levels/stage_1.json'), { genre: 'tower_defence' });
  writeJson(join(dir, 'levels/map-layout.json'), {});

  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'not-a-real-genre',
    level_model: 'authored',
  });
  let result = runGate(dir, 'td_contract');
  assert.equal(result.status, 0, result.stderr);

  rmSync(join(dir, 'levels/stage_1.json'));
  writeJson(join(dir, '.playtest/state.json'), { level_model: 'authored' });
  result = runGate(dir, 'td_contract');
  assert.equal(result.status, 0, result.stderr);

  rmSync(join(dir, 'levels/map-layout.json'));
  result = runGate(dir, 'td_contract');
  assert.equal(result.status, 0, result.stderr);

  writeJson(join(dir, 'levels/map-layout.json'), {});
  writeJson(join(dir, 'levels/stage_1.json'), {
    map_layout_ref: 'levels/map-layout.json',
  });
  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'tower_defence',
    level_model: 'authored',
  });
  result = runGate(dir, 'td_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /map-layout .*invariant violation/);
});

test('composite gates require complete routing metadata', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  writeRunIdentity(dir);
  for (const name of ['state.json', 'resume.json']) {
    const path = join(dir, '.playtest', name);
    const payload = JSON.parse(readFileSync(path, 'utf8'));
    delete payload.genre;
    writeJson(path, payload);
  }
  const result = runGate(dir, 'scaffold_done');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /genre '<missing>' is invalid/);
});

test('tower defence validates every stage effective map precedence', (t) => {
  const dir = fixture(t);
  const configPath = join(dir, 'gca.config.json');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  config.gates.tower_defence = {
    no_baked_path: { enabled: false },
    path_alignment: { enabled: false },
    hud_binding: { enabled: false },
  };
  writeJson(configPath, config);
  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'tower_defence',
    level_model: 'authored',
  });
  const validMap = {
    canvas: { width: 800, height: 600 },
    core: { x: 700, y: 300 },
    spawns: [{ id: 'entry', x: 0, y: 300 }],
    paths: [{
      id: 'main',
      spawn: 'entry',
      waypoints: [{ x: 0, y: 300 }, { x: 700, y: 300 }],
    }],
    build_slots: [{ id: 'slot', x: 350, y: 100, width: 40, height: 40 }],
  };
  writeJson(join(dir, 'levels/map-layout.json'), validMap);
  writeJson(join(dir, 'levels/stage_1.json'), {
    map_layout_ref: 'levels/map-layout.json',
  });
  writeJson(join(dir, 'levels/stage_2.json'), {
    map_layout: {
      ...validMap,
      paths: [{
        id: 'main',
        spawn: 'entry',
        waypoints: [{ x: 0, y: 300 }, { x: 600, y: 300 }],
      }],
    },
  });
  let result = runGate(dir, 'td_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /stage_2\.json#map_layout.*last waypoint/);

  writeJson(join(dir, 'levels/stage_2.json'), { map_layout: validMap });
  result = runGate(dir, 'td_contract');
  assert.equal(result.status, 0, result.stderr);
});

test('tower defence map gates apply only to authored levels', (t) => {
  const dir = fixture(t);
  const configPath = join(dir, 'gca.config.json');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  config.gates.tower_defence = {
    hud_binding: { enabled: false },
  };
  writeJson(configPath, config);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{ key: 'map-base', category: 'background' }],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    'export const waypoint = (spawn, core) => spawn + (core - spawn) * 0.33;\n',
  );

  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'tower_defence',
    level_model: 'none',
  });
  let result = runGate(dir, 'td_contract');
  assert.equal(result.status, 0, result.stderr);

  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'tower_defence',
    level_model: 'authored',
  });
  result = runGate(dir, 'td_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /no levels\/stage_\*\.json exists/);
  assert.match(result.stderr, /not marked "terrain_only": true/);
  assert.match(result.stderr, /no src\/ code builds the enemy path from map-layout/);
  assert.match(result.stderr, /path-interpolation residue/);
});

test('release gate is a declarative strict superset of iteration gate', (t) => {
  const dir = fixture(t);
  const result = runGate(dir, 'gate_contract');
  assert.equal(result.status, 0, result.stderr);
  const contract = JSON.parse(result.stdout);
  assert.equal(contract.strict_superset, true);
  assert.ok(contract.ready_to_ship.includes('critic'));
  assert.ok(contract.ready_to_ship.includes('performance'));
  for (const name of contract.iteration_ready) {
    assert.ok(contract.ready_to_ship.includes(name), name);
  }
});

test('gate config authority fails closed when missing or malformed', (t) => {
  const dir = fixture(t);
  const config = join(dir, 'gca.config.json');
  writeFileSync(config, '{not-json');
  let result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);

  rmSync(config);
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Required config authority is missing/);
});

test('scaffold contract gate validates canonical schemas and helper integrity', (t) => {
  const dir = fixture(t);
  const root = '.claude/skills/game-pipeline/references/scaffold/examples';
  writeJson(join(dir, '.playtest/state.json'), {
    genre: 'puzzle',
    interaction_profile: 'none',
    quality_profile: 'responsive',
    level_model: 'board',
  });
  writeJson(
    join(dir, 'assets/asset-manifest.json'),
    readRepoJson(`${root}/manifest/layout-cluster.json`),
  );
  writeJson(
    join(dir, '.playtest/inputs/interaction-contract.json'),
    readRepoJson(`${root}/contracts/pointer-interaction.json`),
  );
  writeJson(
    join(dir, '.playtest/inputs/objectives.json'),
    readRepoJson(`${root}/contracts/objectives.json`),
  );
  writeJson(
    join(dir, '.playtest/inputs/layout-contract.json'),
    readRepoJson(`${root}/contracts/layout-cluster.json`),
  );
  const displayHelper = join(dir, 'src/display-utils.js');
  mkdirSync(dirname(displayHelper), { recursive: true });
  writeFileSync(
    displayHelper,
    readFileSync(
      join(
        repoRoot,
        '.claude/skills/game-pipeline/references/implement/runtime/display-utils.js',
      ),
    ),
  );

  let result = runGate(dir, 'scaffold_contracts');
  assert.equal(result.status, 0, result.stderr);

  const layoutPath = join(dir, '.playtest/inputs/layout-contract.json');
  const malformedLayout = JSON.parse(readFileSync(layoutPath, 'utf8'));
  malformedLayout.scenes.GameScene.elements[0].box.push(0.5);
  writeJson(layoutPath, malformedLayout);
  result = runGate(dir, 'scaffold_contracts');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /needs at most 4 item/);
  malformedLayout.scenes.GameScene.elements[0].box.pop();
  writeJson(layoutPath, malformedLayout);

  const projectedManifestPath = join(dir, 'assets/asset-manifest.json');
  const projectedManifest = JSON.parse(
    readFileSync(projectedManifestPath, 'utf8'),
  );
  for (const field of ['maxBox', 'logicalBox', 'baseSize']) {
    const invalidManifest = structuredClone(projectedManifest);
    delete invalidManifest.assets[0].maxBox;
    invalidManifest.assets[0][field] = [96, 96, 96];
    writeJson(projectedManifestPath, invalidManifest);
    result = runGate(dir, 'scaffold_contracts');
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /needs at most 2 item/);
  }
  writeJson(projectedManifestPath, projectedManifest);

  rmSync(displayHelper);
  result = runGate(dir, 'scaffold_contracts');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /src\/display-utils\.js is required/);
  writeFileSync(
    displayHelper,
    readFileSync(
      join(
        repoRoot,
        '.claude/skills/game-pipeline/references/implement/runtime/display-utils.js',
      ),
    ),
  );

  const interactionPath = join(
    dir,
    '.playtest/inputs/interaction-contract.json',
  );
  const interaction = JSON.parse(readFileSync(interactionPath, 'utf8'));
  interaction.interaction_profile = 'matter';
  writeJson(interactionPath, interaction);
  result = runGate(dir, 'scaffold_contracts');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must exactly equal state\.json interaction_profile/);
  writeJson(
    interactionPath,
    readRepoJson(`${root}/contracts/pointer-interaction.json`),
  );

  const objectivesPath = join(dir, '.playtest/inputs/objectives.json');
  const objectives = JSON.parse(readFileSync(objectivesPath, 'utf8'));
  objectives.objectives = objectives.objectives.filter((entry) => entry.id !== 'start_game');
  writeJson(objectivesPath, objectives);
  result = runGate(dir, 'scaffold_contracts');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must include the start_game objective/);

  objectives.objectives.unshift(
    readRepoJson(`${root}/contracts/objectives.json`).objectives[0],
  );
  objectives.objectives[0].operation = '   ';
  writeJson(objectivesPath, objectives);
  result = runGate(dir, 'scaffold_contracts');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /operation.*does not match|operation.*non-empty/);
  objectives.objectives[0].operation = 'Start the game';
  objectives.objectives.push({ ...objectives.objectives[0] });
  writeJson(objectivesPath, objectives);
  result = runGate(dir, 'scaffold_contracts');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /duplicate objective id/);
  objectives.objectives.pop();
  writeJson(objectivesPath, objectives);
  const helper = join(dir, 'src/utils/particle-recipes.js');
  mkdirSync(dirname(helper), { recursive: true });
  writeFileSync(helper, 'export const recipes = {};\n');
  result = runGate(dir, 'scaffold_contracts');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /drifted from its canonical helper/);
});

test('asset sanity blocks undersized, duplicate, and undersized-total assets', (t) => {
  const dir = fixture(t);
  const imagePaths = [
    'assets/generated/first.png',
    'assets/generated/second.png',
    'assets/generated/third.png',
  ];
  for (const path of imagePaths) {
    writePngHeader(join(dir, path), 16, 16);
  }
  configureAssetSanity(dir, {
    enabled: true,
    min_png_dimension_px: 32,
    min_unique_files: 3,
    min_total_bytes: 50000,
  });
  writeGeneratedAssetFixture(dir, imagePaths, { width: 16, height: 16 });

  const result = runGate(dir, 'step_done', { step: 3 });
  assert.equal(result.status, 1);
  assert.match(
    result.stderr,
    /3 PNG image\(s\) are below the configured 32px minimum/,
  );
  assert.match(
    result.stderr,
    /found 1 unique image file content hash\(es\), below the configured minimum of 3/,
  );
  assert.match(
    result.stderr,
    /image files total \d+ bytes, below the configured minimum of 50000 bytes/,
  );
});

test('asset sanity accepts configured production-sized assets', (t) => {
  const dir = fixture(t);
  const imagePaths = [
    'assets/generated/first.png',
    'assets/generated/second.png',
    'assets/generated/third.png',
  ];
  imagePaths.forEach((path, index) => {
    const absolutePath = join(dir, path);
    writePngHeader(absolutePath, 32, 32);
    const png = readFileSync(absolutePath);
    const padding = Buffer.concat([
      Buffer.from('padding\0', 'latin1'),
      Buffer.alloc(18000, 0x41 + index),
    ]);
    writeFileSync(absolutePath, Buffer.concat([
      png.subarray(0, -12),
      pngChunk('tEXt', padding),
      png.subarray(-12),
    ]));
  });
  assert.ok(
    imagePaths.reduce((total, path) => total + statSync(join(dir, path)).size, 0)
      >= 50000,
  );
  configureAssetSanity(dir, {
    enabled: true,
    min_png_dimension_px: 32,
    min_unique_files: 3,
    min_total_bytes: 50000,
  });
  writeGeneratedAssetFixture(dir, imagePaths, { width: 32, height: 32 });

  const result = runGate(dir, 'step_done', { step: 3 });
  assert.equal(result.status, 0, result.stderr);
});

test('asset sanity remains opt-in for absent and disabled fixture policies', (t) => {
  const dir = fixture(t);
  const imagePaths = [
    'assets/generated/first.png',
    'assets/generated/second.png',
    'assets/generated/third.png',
  ];
  for (const path of imagePaths) {
    writePngHeader(join(dir, path), 8, 8);
  }
  writeGeneratedAssetFixture(dir, imagePaths, { width: 8, height: 8 });

  let result = runGate(dir, 'step_done', { step: 3 });
  assert.equal(result.status, 0, result.stderr);

  configureAssetSanity(dir, {
    enabled: false,
    min_png_dimension_px: 0,
    min_unique_files: -1,
    min_total_bytes: 'large',
  });
  writeGeneratedAssetFixture(dir, imagePaths, { width: 8, height: 8 });
  result = runGate(dir, 'step_done', { step: 3 });
  assert.equal(result.status, 0, result.stderr);
});

test('asset sanity fails closed on invalid configured thresholds', (t) => {
  const dir = fixture(t);
  const imagePath = 'assets/generated/image.png';
  writePngHeader(join(dir, imagePath), 32, 32);
  configureAssetSanity(dir, {
    enabled: true,
    min_png_dimension_px: 0,
    min_unique_files: 1.5,
    min_total_bytes: -1,
  });
  writeGeneratedAssetFixture(
    dir,
    [imagePath],
    { width: 32, height: 32 },
  );

  const result = runGate(dir, 'step_done', { step: 3 });
  assert.equal(result.status, 1);
  assert.match(
    result.stderr,
    /min_png_dimension_px must be a positive integer when enabled; received 0/,
  );
  assert.match(
    result.stderr,
    /min_unique_files must be a non-negative integer when enabled; received 1.5/,
  );
  assert.match(
    result.stderr,
    /min_total_bytes must be a non-negative integer when enabled; received -1/,
  );
});

test('asset sanity skips a valid no-file-assets contract', (t) => {
  const dir = fixture(t);
  configureAssetSanity(dir, {
    enabled: true,
    min_png_dimension_px: 32,
    min_unique_files: 3,
    min_total_bytes: 50000,
  });
  const manifest = {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: false,
    runtimeSources: ['procedural'],
    assets: [],
  };
  const layout = readRepoJson(
    '.claude/skills/game-pipeline/references/scaffold/examples/contracts/layout-waived-scene.json',
  );
  writeCompleteScaffoldBoundary(dir, { manifest, layout });
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  writeJson(statePath, {
    ...state,
    scenes: { pending: [] },
    assets_generated: true,
  });

  const result = runGate(dir, 'step_done', { step: 3 });
  assert.equal(result.status, 0, result.stderr);
});

test('canonical per-action manifest passes schema validation', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 1,
    assets: [spriteManifestEntry('player', ['idle', 'run'])],
  });
  const result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);
});

test('schema-v2 manifest enforces file source provenance', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  const path = join(dir, 'assets/asset-manifest.json');
  const entry = {
    key: 'ui-logo',
    category: 'ui',
    path: 'assets/ui/logo.png',
    sourceStrategy: 'supplied-image',
    description: 'Supplied UI logo',
    width: 320,
    height: 160,
    maxBox: [320, 160],
  };
  writeJson(path, {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources: [],
    assets: [entry],
  });
  let result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /missing provenance/);

  entry.provenance = { source: 'user-upload' };
  writeJson(path, {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources: [],
    assets: [entry],
  });
  result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);

  writeJson(path, {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources: [],
    assets: [{
      ...spriteManifestEntry('player', ['idle']),
      sourceStrategy: 'supplied-image',
      provenance: { source: 'user-upload' },
    }],
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unsupported supplied-image/);
});

test('supplied and licensed UI PNGs obey profile pixel sizing contracts', (t) => {
  const cases = [
    {
      profile: 'art_directed',
      sourceStrategy: 'supplied-image',
      provenance: { source: 'user-upload' },
      sizing: { targetWidth: 95 },
      fixedSizing: { targetWidth: 96 },
      failure: /PNG targetWidth axis 96 must equal targetWidth 95/,
    },
    {
      profile: 'responsive',
      sourceStrategy: 'licensed-image',
      provenance: { source: 'licensed-pack', licenseId: 'CC0-1.0' },
      sizing: { maxBox: [80, 40] },
      fixedSizing: { maxBox: [96, 40] },
      failure: /PNG dimensions 96×32 must fit maxBox 80×40/,
    },
  ];
  for (const row of cases) {
    const dir = fixture(t);
    const image = 'assets/ui/existing-panel.png';
    writePngHeader(join(dir, image), 96, 32);
    writeJson(join(dir, '.playtest/state.json'), { quality_profile: row.profile });
    const manifestPath = join(dir, 'assets/asset-manifest.json');
    const writeManifest = (sizing) => writeJson(manifestPath, {
      schema_version: 2,
      qualityProfile: row.profile,
      fileAssetsRequired: true,
      runtimeSources: [],
      assets: [{
        key: 'ui-existing-panel',
        category: 'panel',
        sourceStrategy: row.sourceStrategy,
        provenance: row.provenance,
        description: 'Existing panel artwork',
        path: image,
        width: 96,
        height: 32,
        ...sizing,
      }],
    });
    writeManifest(row.sizing);
    let result = runGate(dir, 'asset_manifest');
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, row.failure);
    writeManifest(row.fixedSizing);
    result = runGate(dir, 'asset_manifest');
    assert.equal(result.status, 0, result.stderr);
  }
});

test('static manifest dimensions are required once a runtime PNG exists', (t) => {
  const dir = fixture(t);
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  const entry = {
    key: 'ui-status',
    category: 'ui',
    path: 'assets/ui/status.png',
    sourceStrategy: 'generated-image',
    description: 'Responsive status widget',
    maxBox: [128, 64],
  };
  const writeManifest = () => writeJson(manifestPath, {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources: [],
    assets: [entry],
  });

  writeManifest();
  let result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);

  entry.path = 'assets/ui/status.jpg';
  writeManifest();
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /[.]png/i);
  entry.path = 'assets/ui/status.png';
  writeManifest();

  writePngHeader(join(dir, 'assets/ui/status.png'), 64, 32);
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /positive finalized integer width and height/);

  entry.width = 63;
  entry.height = 32;
  writeManifest();
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /declares 63×32 but PNG is 64×32/);

  entry.width = 64;
  writeManifest();
  result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);

  const imagePath = join(dir, 'assets/ui/status.png');
  const validPng = readFileSync(imagePath);
  const badCrc = Buffer.from(validPng);
  const idatType = badCrc.indexOf(Buffer.from('IDAT'));
  badCrc[idatType + 4] ^= 0x01;
  writeFileSync(imagePath, badCrc);
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a readable PNG/);

  writeFileSync(imagePath, Buffer.concat([
    validPng.subarray(0, 8),
    pngChunk('IHDR', validPng.subarray(16, 29)),
    pngChunk('IDAT', Buffer.concat([
      deflateSync(Buffer.alloc((64 * 4 + 1) * 32)),
      Buffer.from('trailing-zlib-garbage'),
    ])),
    pngChunk('IEND'),
  ]));
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a readable PNG/);

  writeFileSync(imagePath, Buffer.concat([
    validPng.subarray(0, 8),
    pngChunk('IHDR', validPng.subarray(16, 29)),
    pngChunk('IDAT', deflateSync(Buffer.alloc(1))),
    pngChunk('IEND'),
  ]));
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a readable PNG/);

  writeFileSync(imagePath, validPng.subarray(0, validPng.length - 5));
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a readable PNG/);

  const fabricated = Buffer.alloc(24);
  Buffer.from([0x89, 0x50, 0x4e, 0x47]).copy(fabricated);
  fabricated.writeUInt32BE(64, 16);
  fabricated.writeUInt32BE(32, 20);
  writeFileSync(imagePath, fabricated);
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not a readable PNG/);
});

test('all canonical UI alias categories receive profile sizing checks', (t) => {
  const dir = fixture(t);
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });

  for (const category of [
    'bar', 'widget', 'lettering', 'logo', 'wordmark', 'art-word', 'frame', 'overlay',
  ]) {
    writeJson(manifestPath, {
      schema_version: 2,
      qualityProfile: 'responsive',
      fileAssetsRequired: true,
      runtimeSources: [],
      assets: [{
        key: `title-${category}`,
        category,
        path: `assets/ui/title-${category}.png`,
        sourceStrategy: 'generated-image',
        description: `Responsive ${category} art`,
      }],
    });
    const result = runGate(dir, 'asset_manifest');
    assert.notEqual(result.status, 0, `${category}: ${result.stderr}`);
    assert.match(result.stderr, /requires logicalBox\/maxBox/);
  }

  writeJson(manifestPath, {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources: [],
    assets: [{
      key: 'ornate-shell',
      category: 'frame',
      path: 'assets/ui/ornate-shell.png',
      sourceStrategy: 'generated-image',
      description: 'Ornate decorative frame around the full play field',
      decorative: true,
      targetWidth: 900,
    }],
  });
  let decorative = runGate(dir, 'asset_manifest');
  assert.notEqual(decorative.status, 0);
  assert.match(decorative.stderr, /requires logicalBox\/maxBox/);

  const decorativeManifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  delete decorativeManifest.assets[0].targetWidth;
  decorativeManifest.assets[0].maxBox = [900, 500];
  writeJson(manifestPath, decorativeManifest);
  decorative = runGate(dir, 'asset_manifest');
  assert.equal(decorative.status, 0, decorative.stderr);
});

test('asset manifest runtime paths cannot escape assets', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources: [],
    assets: [{
      key: 'ui-panel',
      category: 'ui',
      path: '../outside.png',
      sourceStrategy: 'generated-image',
      description: 'UI panel',
      maxBox: [320, 160],
    }],
  });

  const result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unsafe runtime path/);
});

test('asset manifest rejects browser/file identity-ambiguous paths', (t) => {
  for (const path of [
    'assets/%2e%2e/runtime.png',
    'assets/ui/runtime.png?version=1',
    'assets/ui/runtime.png#fragment',
    'assets\\ui\\runtime.png',
    '/assets/ui/runtime.png',
    'assets/ui/runtime image.png',
    'assets/ui/runtime\u0001.png',
  ]) {
    const dir = fixture(t);
    writeJson(join(dir, 'assets/asset-manifest.json'), {
      assets: [{
        key: 'ui-runtime',
        category: 'ui',
        path,
      }],
    });
    const result = runGate(dir, 'asset_manifest');
    assert.notEqual(result.status, 0, path);
    assert.match(result.stderr, /unsafe runtime path/, path);
  }
});

test('asset manifest rejects symlinked runtime path components', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  writePngHeader(join(dir, 'outside/panel.png'), 64, 32);
  mkdirSync(join(dir, 'assets'), { recursive: true });
  symlinkSync('../outside', join(dir, 'assets/ui'), 'dir');
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources: [],
    assets: [{
      key: 'ui-panel',
      category: 'ui',
      path: 'assets/ui/panel.png',
      sourceStrategy: 'generated-image',
      description: 'Responsive UI panel',
      width: 64,
      height: 32,
      maxBox: [128, 64],
    }],
  });

  const result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /path component 'assets\/ui' is a symlink/);
});

test('schema-v2 UI sizing and runtime sources follow quality profile', (t) => {
  const dir = fixture(t);
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  const baseEntry = {
    key: 'ui-status-panel',
    category: 'panel',
    path: 'assets/ui/status.png',
    sourceStrategy: 'generated-image',
    description: 'Status panel frame',
    width: 96,
    height: 32,
  };
  const writeManifest = (qualityProfile, entry, runtimeSources = []) => writeJson(
    manifestPath,
    {
      schema_version: 2,
      qualityProfile,
      fileAssetsRequired: true,
      runtimeSources,
      assets: [entry],
    },
  );

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  writeManifest('responsive', baseEntry);
  let result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /requires logicalBox\/maxBox/);

  writeManifest('responsive', { ...baseEntry, maxBox: [480, 180] });
  result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);

  writeManifest('responsive', {
    ...baseEntry,
    maxBox: [480, 180],
    logicalBox: [480, 180],
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /exactly one positive pair/);

  writeManifest('responsive', {
    ...baseEntry,
    maxBox: [480, 180],
    targetWidth: 480,
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cannot declare targetWidth/);

  writeManifest('responsive', {
    ...baseEntry,
    maxBox: [480, 180],
    decorative: true,
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /functional UI .* cannot claim decorative:true/);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'pixel_art' });
  writeManifest('pixel_art', {
    ...baseEntry,
    baseSize: [96, 32],
    integerScales: [1, 2, 3],
  });
  result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);

  writeManifest('pixel_art', {
    ...baseEntry,
    baseSize: [96, 32],
    integerScales: [1, 2],
    maxBox: [480, 180],
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cannot declare maxBox/);

  writePngHeader(join(dir, baseEntry.path), 96, 32);
  writeManifest('pixel_art', {
    ...baseEntry,
    baseSize: [95, 32],
    integerScales: [1, 2],
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /PNG dimensions 96×32 must equal baseSize 95×32/);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    schema_version: 1,
    canvas: { designWidth: 1000, designHeight: 500 },
    waived_scenes: [],
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-wide-art',
          role: 'panel',
          kind: 'static',
          box: [0.15, 0.1, 0.7, 0.2],
          sourceStrategy: 'generated-image',
        }],
      },
    },
  });
  writeManifest('art_directed', {
    ...baseEntry,
    targetWidth: 240,
    targetHeight: 72,
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /targetWidth xor targetHeight/);

  writeManifest('art_directed', {
    ...baseEntry,
    targetHeight: 72,
    maxBox: [240, 72],
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cannot declare maxBox/);

  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-health-panel',
          role: 'hud bar',
          kind: 'widget',
          sourceStrategy: 'composition',
        }],
      },
    },
  });
  writeManifest(
    'art_directed',
    { ...baseEntry, targetHeight: 72 },
    ['composition'],
  );
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /art_directed functional widget/);
});

test('art_directed exempts an invisible sensor zone but not a real widget', (t) => {
  const dir = fixture(t);
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  const entry = {
    key: 'ui-status-panel',
    category: 'panel',
    path: 'assets/ui/status.png',
    sourceStrategy: 'generated-image',
    description: 'Status panel frame',
    width: 96,
    height: 32,
    targetHeight: 32,
  };
  writePngHeader(join(dir, entry.path), 96, 32);
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  const writeManifest = () => writeJson(manifestPath, {
    schema_version: 2,
    qualityProfile: 'art_directed',
    fileAssetsRequired: true,
    runtimeSources: ['procedural'],
    assets: [entry],
  });
  const writeLayout = (element) => writeJson(
    join(dir, '.playtest/inputs/layout-contract.json'),
    {
      schema_version: 1,
      canvas: { designWidth: 1000, designHeight: 500 },
      waived_scenes: [],
      scenes: { GameScene: { elements: [element] } },
    },
  );

  // flappy-bird's pass sensor: a region with no pixels of its own. The widget
  // label test reads the key alone, so `score-gate` was demanded as art nobody
  // could ever see, and the run had no way past the gate.
  writeManifest();
  writeLayout({
    key: 'score-gate',
    role: 'sensor zone',
    kind: 'zone',
    box: [0.4, 0.0, 0.05, 1.0],
    sourceStrategy: 'procedural',
  });
  let result = runGate(dir, 'asset_manifest');
  assert.doesNotMatch(result.stderr, /art_directed functional widget/);

  // Same word, but bound to a value, so it displays something and still owes art.
  writeManifest();
  writeLayout({
    key: 'score-gate',
    role: 'hud',
    kind: 'zone',
    bind: 'score',
    box: [0.4, 0.0, 0.05, 1.0],
    sourceStrategy: 'procedural',
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /art_directed functional widget/);

  // A genuine HUD widget drawn with primitives stays blocked.
  writeManifest();
  writeLayout({
    key: 'ui-health-bar',
    role: 'hud bar',
    kind: 'widget',
    box: [0.1, 0.1, 0.3, 0.05],
    sourceStrategy: 'procedural',
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /art_directed functional widget/);
});


test('explicit non-file profile contract permits an empty asset manifest', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'minimal' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [
          { key: 'playfield', sourceStrategy: 'composition' },
          { key: 'score', sourceStrategy: 'live-text' },
        ],
      },
    },
  });
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 2,
    qualityProfile: 'minimal',
    fileAssetsRequired: false,
    runtimeSources: ['composition', 'live-text'],
    assets: [],
  });
  let result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);

  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [
          { key: 'required-background', sourceStrategy: 'generated-image' },
          { key: 'score', sourceStrategy: 'live-text' },
        ],
      },
    },
  });
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 2,
    qualityProfile: 'minimal',
    fileAssetsRequired: false,
    runtimeSources: ['live-text'],
    assets: [],
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /file-backed (?:layout obligations|keys)/);

  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'status',
          sourceStrategy: 'composition',
          children: [
            {
              kind: 'image',
              key: 'required-icon',
              sourceStrategy: 'generated-image',
            },
            {
              kind: 'text',
              bind: 'score',
              sourceStrategy: 'live-text',
            },
          ],
        }],
      },
    },
  });
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 2,
    qualityProfile: 'minimal',
    fileAssetsRequired: false,
    runtimeSources: ['composition', 'live-text'],
    assets: [],
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /file-backed (?:layout obligations|keys)/);

  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [
          { key: 'playfield', sourceStrategy: 'composition' },
          { key: 'score', sourceStrategy: 'live-text' },
        ],
      },
    },
  });
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 2,
    qualityProfile: 'minimal',
    fileAssetsRequired: false,
    runtimeSources: ['bitmap-text'],
    assets: [],
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);

  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 2,
    qualityProfile: 'minimal',
    fileAssetsRequired: false,
    runtimeSources: ['composition', 'live-text'],
    assets: [],
  });
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /no file assets without a valid non-art profile/);
});

test('layout source contract rejects duplicate key and bind identities', (t) => {
  const dir = fixture(t);
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  const layoutPath = join(dir, '.playtest/inputs/layout-contract.json');
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  const writeManifest = (runtimeSources) => writeJson(manifestPath, {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources,
    assets: [{
      key: 'ui-panel',
      category: 'ui',
      path: 'assets/ui/panel.png',
      sourceStrategy: 'generated-image',
      description: 'Responsive panel art',
      width: 100,
      height: 50,
      maxBox: [200, 100],
    }],
  });
  const cases = [
    {
      runtimeSources: [],
      duplicate: 'ui-panel',
      elements: [
        { key: 'ui-panel', sourceStrategy: 'generated-image' },
        { key: 'ui-panel', sourceStrategy: 'generated-image' },
      ],
    },
    {
      runtimeSources: ['live-text'],
      duplicate: 'score',
      elements: [
        { key: 'score', bind: 'score', sourceStrategy: 'live-text' },
        { key: 'score-copy', bind: 'score', sourceStrategy: 'live-text' },
      ],
    },
    {
      runtimeSources: ['live-text'],
      duplicate: 'ui-panel',
      elements: [
        { key: 'ui-panel', sourceStrategy: 'generated-image' },
        { bind: 'ui-panel', sourceStrategy: 'live-text' },
      ],
    },
  ];

  for (const entry of cases) {
    writeManifest(entry.runtimeSources);
    writeJson(layoutPath, {
      scenes: { GameScene: { elements: entry.elements } },
    });
    const result = runGate(dir, 'asset_manifest');
    assert.notEqual(result.status, 0);
    assert.match(
      result.stderr,
      new RegExp(`logical identity '${entry.duplicate}'.*duplicates`),
    );
  }

  writeManifest([]);
  writeJson(layoutPath, {
    scenes: {
      TitleScene: {
        elements: [
          { key: 'ui-panel', sourceStrategy: 'generated-image' },
        ],
      },
      GameScene: {
        elements: [
          { key: 'ui-panel', sourceStrategy: 'generated-image' },
        ],
      },
    },
  });
  let result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);

  writeJson(layoutPath, {
    scenes: {
      TitleScene: {
        elements: [
          { key: 'ui-panel', sourceStrategy: 'generated-image' },
        ],
      },
      GameScene: {
        elements: [
          { key: 'ui-panel', sourceStrategy: 'supplied-image' },
        ],
      },
    },
  });
  result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /identity 'ui-panel'.*conflicts.*across scenes/);
});

test('bitmap-text source contracts are rejected on every schema surface', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'minimal' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'score',
          bind: 'score',
          sourceStrategy: 'bitmap-text',
          fontSource: 'runtime',
        }],
      },
    },
  });
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    schema_version: 2,
    qualityProfile: 'minimal',
    fileAssetsRequired: false,
    runtimeSources: ['bitmap-text'],
    assets: [],
  });
  const result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /bitmap-text.*unsupported/);

  const manifestSchema = readRepoJson(
    '.claude/skills/game-pipeline/references/scaffold/schemas/asset-manifest.schema.json',
  );
  assert.doesNotMatch(JSON.stringify(manifestSchema), /bitmap-text/);
  const layoutSchema = readRepoJson(
    '.claude/skills/game-pipeline/references/scaffold/schemas/layout-contract.schema.json',
  );
  assert.doesNotMatch(JSON.stringify(layoutSchema), /bitmap-text/);
});

test('manifest rejects per-action metadata with a different basename', (t) => {
  const dir = fixture(t);
  const player = spriteManifestEntry('player');
  player.actions.idle.metadata = 'assets/sprites/player/wrong.json';
  writeJson(join(dir, 'assets/asset-manifest.json'), { assets: [player] });
  const result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /same-basename/);
});

test('nested plural sprite maps normalize to canonical sprite entries', (t) => {
  const dir = fixture(t);
  const player = spriteManifestEntry('player', ['idle', 'run']);
  delete player.category;
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    sprites: { player },
  });
  const result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 0, result.stderr);

  const image = join(dir, player.actions.idle.path);
  writePngHeader(image, 16, 16);
  const gddPath = join(dir, 'GAME_DESIGN.md');
  writeFileSync(
    gddPath,
    '### 2A: Character Plan\n| Key | Action | Description |\n'
      + '|---|---|---|\n| player | idle | player design |\n'
      + '| player | run | player run design |\n',
  );
  writeJson(join(dir, '.playtest/verify_middle/slot/player.json'), {
    schema_version: 3,
    producer: 'gca-mcp.validate_asset_slot',
    key: 'player',
    action: 'idle',
    image,
    image_sha256: sha256(image),
    gdd_sha256: sha256(gddPath),
    gdd_slot_context_sha256: gddSlotContextSha256(
      readFileSync(gddPath, 'utf8'),
      'player',
      player.description,
      'idle',
    ),
    slot_contract_sha256: slotContractSha256({ ...player, category: 'sprite' }),
    slot_description_sha256: createHash('sha256')
      .update(player.description)
      .digest('hex'),
    passed: true,
    verdict: 'PASS',
  });
  const slot = runGate(dir, 'slot_validation');
  assert.equal(slot.status, 0, slot.stderr);

  const manifestPath = join(dir, 'assets/asset-manifest.json');
  writeJson(join(dir, '.playtest/verify/assets/asset-integration-report.json'), {
    schema_version: 2,
    producer: 'gca-mcp.write_asset_integration_report',
    manifest_sha256: sha256(manifestPath),
    checks: [{ key: 'player', wired: true, files: [] }],
    blocking_issues: [],
    summary: {
      manifest_entries: 1,
      blocking_issue_count: 0,
    },
  });
  const phase = runGate(dir, 'step_done', { step: 4 });
  assert.doesNotMatch(
    phase.stderr,
    /asset-integration-report\.json is not a valid MCP-owned integration receipt/,
  );
});

test('nested manifest entries still receive aspect and matting checks', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  writePngHeader(join(dir, 'assets/ui/logo.png'), 100, 100);
  writeRgbPng(join(dir, 'assets/ui/badge.png'), 32, 32);
  writePngHeader(join(dir, 'design/crops/title/logo.png'), 200, 50);
  writeFileSync(
    join(dir, 'GAME_DESIGN.md'),
    '| logo | design/crops/title/logo.png |\n',
  );
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    ui: {
      logo: {
        sourceStrategy: 'generated-image',
        description: 'decorative logo',
        path: 'assets/ui/logo.png',
        decorative: true,
      },
      badge: {
        sourceStrategy: 'generated-image',
        description: 'status badge',
        path: 'assets/ui/badge.png',
      },
    },
  });

  const result = runGate(dir, 'iteration_ready');
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stderr, /aspect drift: 1 lettering\/UI asset/);
  assert.match(result.stderr, /matting incomplete: 1 non-scene asset/);
});

test('normalized manifests cannot bypass bbox or FX policy checks', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  writeJson(join(dir, '.playtest/state.json'), {
    quality_profile: 'art_directed',
  });
  writeJson(join(dir, 'assets/asset-manifest.json'), [
    {
      key: 'ledge',
      category: 'platform',
      path: 'assets/platforms/ledge.png',
    },
    {
      key: 'fx-smoke',
      category: 'fx',
      path: 'assets/fx/smoke.png',
    },
    {
      key: 'decorative-strip',
      category: 'tile',
      decorative: true,
      cells: [{}],
    },
  ]);

  const result = runGate(dir, 'iteration_ready');
  assert.equal(result.status, 1, result.stderr);
  assert.match(
    result.stderr,
    /asset-manifest\.json: 1 collidable static asset\(s\) missing 'bbox'/,
  );
  assert.match(result.stderr, /per-FX \/ atom artefact\(s\) found/);
});

test('nested NPC maps are normalized and cannot bypass sprite schema', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    npcs: {
      merchant: { path: 'assets/sprites/merchant/merchant.png' },
    },
  });
  const result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /sprite 'merchant' must declare canonical actions/);
});

test('top-level array manifest rejects primitive entries', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), ['assets/ui/icon.png']);
  const result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /entries must be objects/);
});

test('manifest rejects texture collisions across sprite and static assets', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [
      spriteManifestEntry('player'),
      {
        key: 'ui-icon',
        category: 'ui',
        textureKey: 'player-idle',
        path: 'assets/ui/icon.png',
      },
    ],
  });
  const result = runGate(dir, 'asset_manifest');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /duplicates Phaser texture key 'player-idle'/);
});

test('manifest requires every final asset to be runtime-referenced', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [
      {
        key: 'ui-used',
        category: 'ui',
        path: 'assets/ui/used.png',
        width: 1,
        height: 1,
      },
      {
        key: 'ui-unused',
        category: 'ui',
        path: 'assets/ui/unused.png',
        width: 1,
        height: 1,
      },
    ],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/main.js'),
    "scene.load.image('ui-used', 'assets/ui/used.png');\n",
  );

  const structural = runGate(dir, 'asset_manifest');
  assert.equal(structural.status, 0, structural.stderr);

  const unused = runGate(dir, 'asset_runtime_references');
  assert.equal(unused.status, 1);
  assert.match(unused.stderr, /ui-unused/);

  writeFileSync(
    join(dir, 'src/main.js'),
    "scene.load.image('ui-used', 'assets/ui/used.png');\n"
      + "// ui-unused assets/ui/unused.png\n",
  );
  const commentOnly = runGate(dir, 'asset_runtime_references');
  assert.equal(commentOnly.status, 1);
  assert.match(commentOnly.stderr, /ui-unused/);

  writeFileSync(
    join(dir, 'src/main.js'),
    "fetch('assets/asset-manifest.json').then(loadAllManifestAssets);\n",
  );
  const fakeManifestLoader = runGate(dir, 'asset_runtime_references');
  assert.equal(fakeManifestLoader.status, 1);
  assert.match(fakeManifestLoader.stderr, /ui-used/);

  writeFileSync(
    join(dir, 'src/main.js'),
    "const manifest = await fetch('assets/asset-manifest.json').then(r => r.json());\n"
      + "for (const entry of manifest.assets) {\n"
      + "  scene.load.image(\n"
      + "    entry.textureKey || entry.key,\n"
      + "    entry.path,\n"
      + "  );\n"
      + "}\n",
  );
  const realManifestLoader = runGate(dir, 'asset_runtime_references');
  assert.equal(realManifestLoader.status, 0, realManifestLoader.stderr);
});

test('flat display params ignore ui and uiCanvas reserved keys', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    schema_version: 1,
    player: measuredEntity(),
    uiCanvas: { w: 1280, h: 720 },
    ui: [{ key: 'ui-health', width: 200, height: 30 }],
  });
  const result = runGate(dir, 'entity_scale');
  assert.equal(result.status, 0, result.stderr);
});

test('body validation cannot be bypassed by omitting contentBbox', (t) => {
  const dir = fixture(t);
  const player = measuredEntity();
  delete player.contentBbox;
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: { player },
  });
  const result = runGate(dir, 'entity_scale');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /content_bbox_missing_for_body|requires a finite positive contentBbox/);
});

test('body validation rejects a finite but displaced offset', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: {
      player: measuredEntity({
        bodyWidth: 50,
        bodyHeight: 50,
        bodyOffsetX: 1000,
        bodyOffsetY: 1000,
      }),
    },
  });
  const result = runGate(dir, 'entity_scale');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /body rect .* is not aligned/);
});

test('measured bodies must declare their coordinate space', (t) => {
  const dir = fixture(t);
  const player = measuredEntity();
  delete player.bodySpace;
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: { player },
  });
  const result = runGate(dir, 'entity_scale');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /require bodySpace 'frame' or 'world'/);
});

test('pick gate uses scaled visible geometry rather than local body size', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: {
      star: measuredEntity({
        scale: 0.2,
        bodyWidth: undefined,
        bodyHeight: undefined,
        bodyOffsetX: undefined,
        bodyOffsetY: undefined,
      }),
    },
  });
  writeInteractionContract(dir, {
    layers: ['pointer', 'star'],
    entities: {
      star: { layer: 'star', shape: 'circle', body: 'none', fit: 'bbox', min_hit_px: 40 },
    },
    matrix: [['pointer', 'star', 'pick']],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(join(dir, 'src/GameScene.js'), "this.input.on('pointerdown', () => {});\n");
  const result = runGate(dir, 'interaction_contract');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /pick target 'star' hit 20px < required 40px/);
});

test('tunnel gate converts frame-local bodies to world units', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: {
      player_bullet: measuredEntity({
        scale: 0.05,
        contentBbox: { x: 0, y: 0, w: 20, h: 20 },
        bodyWidth: 20,
        bodyHeight: 20,
      }),
      enemy: measuredEntity({ scale: 0.05 }),
    },
  });
  writeInteractionContract(dir, {
    layers: ['player_attack', 'enemy'],
    entities: {
      player_bullet: {
        layer: 'player_attack', shape: 'circle', body: 'dynamic', fit: 'bbox', bullet: true,
      },
      enemy: { layer: 'enemy', shape: 'rect', body: 'dynamic', fit: 'bbox' },
    },
    matrix: [['player_attack', 'enemy', 'overlap']],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/config.js'),
    'export const CONFIG = { bulletSpeed: 600 };\nregisterInteractions(this, groups, contract);\n',
  );
  const result = runGate(dir, 'interaction_contract');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /travels 10px\/frame.*smallest target body 5px/);
});

test('interaction contract rejects entity layers outside the declared topology', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: { enemy: measuredEntity() },
  });
  writeInteractionContract(dir, {
    layers: ['enemy'],
    entities: {
      enemy: { layer: 'enemyy', shape: 'rect', body: 'dynamic', fit: 'bbox' },
    },
    matrix: [['enemy', 'enemy', 'ignore']],
  });
  const result = runGate(dir, 'interaction_contract');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /references undeclared layer 'enemyy'/);
});

test('pointer entities require a non-empty pick topology', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: {
      star: measuredEntity({
        bodyWidth: undefined,
        bodyHeight: undefined,
        bodyOffsetX: undefined,
        bodyOffsetY: undefined,
        bodySpace: undefined,
      }),
    },
  });
  writeInteractionContract(dir, {
    layers: [],
    actions: {
      select: { primitive: 'pointer_down' },
    },
    entities: {
      star: { layer: 'star', shape: 'circle', body: 'none', fit: 'bbox' },
    },
    matrix: [],
  });
  const result = runGate(dir, 'interaction_contract');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /pointer actions require non-empty layers \+ matrix/);
});

test('non-interacting board entities may omit interaction topology', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: {
      tile: measuredEntity({
        bodyWidth: undefined,
        bodyHeight: undefined,
        bodyOffsetX: undefined,
        bodyOffsetY: undefined,
        bodySpace: undefined,
      }),
    },
  });
  writeInteractionContract(dir, {
    layers: [],
    actions: {},
    entities: {
      tile: { layer: 'board', shape: 'rect', body: 'none', fit: 'bbox' },
    },
    matrix: [],
  });
  const result = runGate(dir, 'interaction_contract');
  assert.equal(result.status, 0, result.stderr);
});

test('interaction matrix rejects duplicate or conflicting layer pairs', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    entities: {
      player: measuredEntity(),
      enemy: measuredEntity(),
    },
  });
  writeInteractionContract(dir, {
    layers: ['player', 'enemy'],
    entities: {
      player: { layer: 'player', shape: 'rect', body: 'dynamic', fit: 'bbox' },
      enemy: { layer: 'enemy', shape: 'rect', body: 'dynamic', fit: 'bbox' },
    },
    matrix: [
      ['player', 'enemy', 'collide'],
      ['enemy', 'player', 'overlap'],
    ],
  });
  const result = runGate(dir, 'interaction_contract');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /declared more than once \(collide and overlap\)/);
});

test('slot validation requires one report for every sprite key', (t) => {
  const dir = fixture(t);
  const image = join(dir, 'assets/sprites/player/reference.png');
  writePngHeader(image, 16, 16);
  writeFileSync(
    join(dir, 'GAME_DESIGN.md'),
    '### 2A: Character Plan\n| Key | Action | Description |\n'
      + '|---|---|---|\n| player | idle | player design |\n'
      + '| enemy | idle | enemy design |\n',
  );
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [spriteManifestEntry('player'), spriteManifestEntry('enemy')],
  });
  writeSlotReceipt(dir, 'player', image);
  const result = runGate(dir, 'slot_validation');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /missing for 1 generated asset\(s\): enemy/);
});

test('slot validation accepts exact static generated runtime artifacts', (t) => {
  const dir = fixture(t);
  const image = join(dir, 'assets/ui/panel.png');
  writePngHeader(image, 16, 16);
  writeFileSync(
    join(dir, 'GAME_DESIGN.md'),
    '### 2E: UI Registry\n| Key | Description |\n'
      + '|---|---|\n| panel | blue results panel |\n',
  );
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'panel',
      category: 'ui',
      sourceStrategy: 'generated-image',
      path: 'assets/ui/panel.png',
      description: 'blue results panel',
    }],
  });
  writeSlotReceipt(dir, 'panel', image);

  const result = runGate(dir, 'slot_validation');
  assert.equal(result.status, 0, result.stderr);
});

test('GDD slot context hash matches the cross-runtime golden vector', () => {
  const gdd = '### 2A: Character Plan\r'
    + '| Key | Action | Description |\u2028'
    + '|---|---|---|\r\n'
    + '| `hero` | idle | blue  armored\thero |\n';
  assert.equal(
    gddSlotContextSha256(gdd, 'hero', 'blue armored hero', 'idle'),
    '2f150fee9151b743ce6d539322bf1841599087da9896d87ef25e842aaa664b15',
  );
  assert.equal(
    gddSlotContextSha256(
      `${gdd}| hero | idle | obsolete blue armored hero |\n`,
      'hero',
      'blue armored hero',
      'idle',
    ),
    null,
  );
  const staleSecondaryTable = '### 2A: Character Plan\n'
    + '| Key | Action | Description |\n'
    + '|---|---|---|\n'
    + '| hero | run | red armored hero |\n\n'
    + '**Changelog**\n'
    + '| Key | Action | Description |\n'
    + '|---|---|---|\n'
    + '| hero | idle | blue armored hero |\n';
  assert.equal(
    gddSlotContextSha256(
      staleSecondaryTable,
      'hero',
      'blue armored hero',
      'idle',
    ),
    null,
  );
});

test('slot validation rejects evidence after image or GDD changes', (t) => {
  const dir = fixture(t);
  const image = join(dir, 'assets/sprites/player/idle-sheet.png');
  const gdd = join(dir, 'GAME_DESIGN.md');
  writePngHeader(image, 16, 16);
  writeFileSync(
    gdd,
    '### 2A: Character Plan\n| Key | Action | Description |\n'
      + '|---|---|---|\n| player | idle | player design |\n',
  );
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [spriteManifestEntry('player')],
  });
  const writeReport = () => writeSlotReceipt(dir, 'player', image);
  writeReport();
  let result = runGate(dir, 'slot_validation');
  assert.equal(result.status, 0, result.stderr);

  const reducedManifest = JSON.parse(
    readFileSync(join(dir, 'assets/asset-manifest.json'), 'utf8'),
  );
  reducedManifest.assets.push({
    key: 'ui-frame',
    category: 'ui',
    sourceStrategy: 'procedural',
    description: 'unrelated UI frame',
    path: 'assets/ui/frame.png',
    width: 120,
    height: 40,
  });
  writeJson(join(dir, 'assets/asset-manifest.json'), reducedManifest);
  result = runGate(dir, 'slot_validation');
  assert.equal(
    result.status,
    0,
    'unrelated reducer fields/entries must not invalidate a sprite slot receipt',
  );

  writePngHeader(image, 32, 16);
  result = runGate(dir, 'slot_validation');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /stale or unbound to its current image/);

  writeReport();
  writeFileSync(
    gdd,
    '### 2A: Character Plan\n| Key | Action | Description |\n'
      + '|---|---|---|\n| player | idle | player design |\n\n'
      + '# Unrelated prose changed\nnotes\n',
  );
  result = runGate(dir, 'slot_validation');
  assert.equal(
    result.status,
    0,
    'unrelated GDD edits must not invalidate an unchanged slot description',
  );

  writeFileSync(
    gdd,
    '### 2A: Character Plan\n| Key | Action | Description |\n'
      + '|---|---|---|\n| player | idle | updated player design |\n\n'
      + '# Changelog\nplayer | idle | player design\n',
  );
  result = runGate(dir, 'slot_validation');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /stale relative to GAME_DESIGN/);
});

test('subject-margin gate recognizes legacy combined sidecar names', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/sprites/player/player-spritesheet.json'), {
    frameWidth: 100,
    frameHeight: 100,
    contentBbox: { x: 0, y: 10, w: 90, h: 80 },
    facing: 'right',
  });
  const result = runGate(dir, 'subject_margin');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /player-spritesheet\.json/);
});

test('slot discovery ignores archived sprite directories', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/sprites/.archived_old/idle-sheet.json'), {
    frameWidth: 100,
    frameHeight: 100,
    contentBbox: { x: 10, y: 10, w: 80, h: 80 },
  });
  const result = runGate(dir, 'slot_validation');
  assert.equal(result.status, 0, result.stderr);
});

test('crop coverage requires screen-qualified paths for duplicate filenames', (t) => {
  const dir = fixture(t);
  writeFileSync(
    join(dir, 'GAME_DESIGN.md'),
    'Ref Crop: design/crops/title/background.png\n',
  );
  for (const screen of ['title', 'gameplay']) {
    writeCropEvidence(dir, screen, [{
        label: 'background',
        crop: 'background.png',
        source: 'full_mockup',
        box_px: [0, 0, 1280, 720],
        width: 1280,
        height: 720,
      }]);
  }

  const incomplete = runGate(dir, 'crop_coverage');
  assert.equal(incomplete.status, 1);
  assert.match(incomplete.stderr, /gameplay\/background\.png/);

  writeFileSync(
    join(dir, 'GAME_DESIGN.md'),
    'Ref Crop: design/crops/title/background.png\n'
      + 'Ref Crop: design/crops/gameplay/background.png\n',
  );
  const complete = runGate(dir, 'crop_coverage');
  assert.equal(complete.status, 0, complete.stderr);

  const gameplayManifest = join(dir, 'design/crops/gameplay/crops.json');
  const detectedOnly = JSON.parse(readFileSync(gameplayManifest, 'utf8'));
  detectedOnly.crops[0].source = 'detect';
  writeJson(gameplayManifest, detectedOnly);
  const missingFullMockup = runGate(dir, 'crop_coverage');
  assert.equal(missingFullMockup.status, 1);
  assert.match(missingFullMockup.stderr, /missing the required full-mockup background crop/);
});

test('crop integrity rejects tampering, stale lineage, and malformed evidence', (t) => {
  const dir = fixture(t);
  const cropRel = 'design/crops/gameplay/player.png';
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  writeFileSync(join(dir, 'GAME_DESIGN.md'), `Ref Crop: ${cropRel}\n`);
  let manifestPath = writeCropEvidence(dir, 'gameplay', [{
    label: 'player',
    crop: 'player.png',
    required: true,
  }]);

  let result = runGate(dir, 'crop_coverage');
  assert.equal(result.status, 0, result.stderr);

  rmSync(join(dir, 'design/gameplay.png'));
  result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /source file is missing/);

  manifestPath = writeCropEvidence(dir, 'gameplay', [{
    label: 'player',
    crop: 'player.png',
    required: true,
  }]);
  rmSync(join(dir, 'design/gameplay.detect.json'));
  result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /detection file is missing/);

  manifestPath = writeCropEvidence(dir, 'gameplay', [{
    label: 'player',
    crop: 'player.png',
    required: true,
  }]);
  writePngHeader(join(dir, cropRel), 101, 40);
  result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /crop.*sha256 is stale or tampered/);

  manifestPath = writeCropEvidence(dir, 'gameplay', [{
    label: 'player',
    crop: 'player.png',
    required: true,
  }]);
  writeFileSync(join(dir, 'design/gameplay.png'), 'stale source');
  result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /source.*sha256 is stale or tampered/);

  manifestPath = writeCropEvidence(dir, 'gameplay', [{
    label: 'player',
    crop: 'player.png',
    required: true,
  }]);
  writeFileSync(join(dir, 'design/gameplay.detect.json'), '{"stale":true}');
  result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /detection.*sha256 is stale or tampered/);

  manifestPath = writeCropEvidence(dir, 'gameplay', [{
    label: 'player',
    crop: 'player.png',
    required: true,
  }]);
  const evidence = JSON.parse(readFileSync(manifestPath, 'utf8'));
  delete evidence.crops[0].sha256;
  writeJson(manifestPath, evidence);
  result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /crop rows require path \+ sha256/);

  writeFileSync(manifestPath, '{ malformed');
  result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /missing or malformed JSON/);

  rmSync(manifestPath);
  result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /no matching current crops\.json row\/hash/);
});

test('quality profile scopes optional crop and static-text obligations', (t) => {
  const dir = fixture(t);
  writeFileSync(join(dir, 'GAME_DESIGN.md'), '# game\n');
  writeCropEvidence(dir, 'gameplay', [{
      label: 'optional-label',
      crop: 'optional-label.png',
      box_px: [0, 0, 100, 30],
      required: false,
      width: 100,
      height: 30,
    }], { detect: false });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    "export function draw(scene) { scene.add.text(10, 10, 'SCORE'); }\n",
  );

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  let result = runGate(dir, 'crop_coverage');
  assert.notEqual(result.status, 0);
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /static text/i);

  writeFileSync(
    join(dir, 'src/Scene.js'),
    "export function draw(scene) { scene.add.bitmapText(10, 10, 'font', 'SCORE'); }\n",
  );
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /bitmap-text.*unsupported/i);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  result = runGate(dir, 'crop_coverage');
  assert.equal(result.status, 0, result.stderr);
  writeFileSync(
    join(dir, 'src/Scene.js'),
    "export function draw(scene, score) { return scene.add.text(10, 10, score); }\n",
  );
  result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);
});

test('art-directed functional widgets cannot use runtime composition', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-health-bar',
          kind: 'widget',
          role: 'health-bar',
          sourceStrategy: 'composition',
        }],
      },
    },
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    "export function draw(scene) { return scene.add.container(0, 0).setName('ui-health-bar'); }\n",
  );

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  let result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /functional widget frames as file-backed art/);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /display-params\.json is missing/);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 320, h: 180 },
    ui: [{ key: 'other-widget', width: 20, height: 20, x: 20, y: 20 }],
  });
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /omits 1 required UI key/);
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 320, h: 180 },
    ui: [{ key: 'ui-health-bar', width: 120, height: 20, x: 70, y: 20 }],
  });
  result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);
});

test('minimal scene coverage requires references and validates exact waivers', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  const contractPath = join(dir, '.playtest/inputs/layout-contract.json');
  writeJson(contractPath, {
    scenes: {
      GameScene: { reference: 'design/gameplay.png', elements: [] },
      MenuScene: { elements: [] },
    },
    waived_scenes: [],
  });

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'minimal' });
  let result = runGate(dir, 'scene_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /MenuScene/);

  writeJson(contractPath, {
    scenes: {
      GameScene: { reference: 'design/gameplay.png', elements: [] },
      MenuScene: { reference: 'design/gameplay.png', elements: [] },
    },
    waived_scenes: [],
  });
  result = runGate(dir, 'scene_coverage');
  assert.equal(result.status, 0, result.stderr);

  rmSync(join(dir, 'design/gameplay.png'));
  writeJson(contractPath, {
    scenes: {
      GameScene: {
        reference: 'waived:concept-generation-unavailable',
        elements: [],
      },
      MenuScene: { reference: 'waived:pure-transition', elements: [] },
    },
    waived_scenes: ['GameScene', 'MenuScene'],
  });
  result = runGate(dir, 'scene_coverage');
  assert.equal(result.status, 0, result.stderr);

  writeJson(contractPath, {
    scenes: {
      GameScene: { reference: 'waived:cost-cap', elements: [] },
    },
    waived_scenes: ['GameScene'],
  });
  result = runGate(dir, 'scene_coverage');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not an allowed exact waiver/);
});

test('UI scaling policy distinguishes native responsive and pixel profiles', (t) => {
  const dir = fixture(t);
  writePngHeader(join(dir, 'assets/ui/icon.png'), 16, 16);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-icon',
      category: 'ui',
      path: 'assets/ui/icon.png',
      targetWidth: 16,
      baseSize: [16, 16],
      integerScales: [1, 2],
    }],
  });
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 320, h: 180 },
    ui: [{ key: 'ui-icon', width: 30, height: 30, x: 30, y: 30 }],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    "export function draw(scene) { return scene.add.image(30, 30, 'ui-icon'); }\n",
  );
  writeFileSync(
    join(dir, 'src/main.js'),
    'export const game = new Phaser.Game({ pixelArt: true });\n',
  );

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  let result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'pixel_art' });
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /integer uniform scale/);

  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 320, h: 180 },
    ui: [{ key: 'ui-icon', width: 32, height: 32, x: 30, y: 30 }],
  });
  result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);

  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-icon',
      category: 'ui',
      path: 'assets/ui/icon.png',
      targetWidth: 16,
      baseSize: [16, 16],
      integerScales: [1, 3],
    }],
  });
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not declared in manifest integerScales/);

  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-icon',
      category: 'ui',
      path: 'assets/ui/icon.png',
      targetWidth: 16,
      baseSize: [16, 16],
      integerScales: [1, 2],
    }],
  });
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /renders UI 1:1/);
});

test('pixel_art runtime trusts captured game config instead of source text', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/main.js'),
    'const unrelated = { pixelArt: true };\n'
      + 'export const game = new Phaser.Game({ pixelArt: false });\n',
  );
  const identity = writeRunIdentity(dir, { qualityProfile: 'pixel_art' });
  const report = writeVerifyReport(dir, identity);
  let result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /captured runtime game\.config\.pixelArt === true/);

  const registryPath = join(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  registry.game_config.pixelArt = true;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);
  result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);
  result = runGate(dir, 'verify_report');
  assert.equal(result.status, 0, result.stderr);
});

test('profile-dependent focused gates reject unknown quality profiles', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/main.js'),
    'export const game = new Phaser.Game({ pixelArt: true });\n',
  );
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'pixel-art' });

  const result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /quality_profile 'pixel-art' is invalid/);
});

test('UI scale contract is config-driven, role-explicit, and per-axis', (t) => {
  const dir = fixture(t);
  const configPath = join(dir, 'gca.config.json');
  writePngHeader(join(dir, 'assets/ui/panel.png'), 700, 100);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-wide-art',
      category: 'ui',
      path: 'assets/ui/panel.png',
      targetWidth: 700,
    }],
  });
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1000, designHeight: 500 },
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-wide-art',
          role: 'panel',
          kind: 'static',
          box: [0.15, 0.1, 0.7, 0.2],
        }],
      },
    },
  });
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 1000, h: 500 },
    ui: [{
      key: 'ui-wide-art',
      role: 'panel',
      width: 700,
      height: 100,
      x: 500,
      y: 100,
    }],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    "export function draw(scene) { return scene.add.image(500, 100, 'ui-wide-art'); }\n",
  );

  let result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);
  const auditPath = join(
    dir,
    '.playtest/verify_middle/ui-scale/ui-scale-report.json',
  );
  let audit = JSON.parse(readFileSync(auditPath, 'utf8'));
  assert.equal(audit.schema_version, 2);
  assert.equal(audit.passed, true);

  const displayPath = join(dir, '.playtest/inputs/display-params.json');
  const display = JSON.parse(readFileSync(displayPath, 'utf8'));
  display.uiCanvas.w = 10000;
  writeJson(displayPath, display);
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not match layout-contract canvas/);
  display.uiCanvas.w = 1000;

  display.ui[0].decorative = true;
  writeJson(displayPath, display);
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cannot grant decorative exemption/);
  delete display.ui[0].decorative;

  display.ui[0].role = 'results panel';
  writeJson(displayPath, display);
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not match authored role/);

  delete display.ui[0].role;
  writeJson(displayPath, display);
  result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);

  display.ui[0].role = 'panel';
  display.ui[0].width = 1024;
  display.ui[0].height = 100;
  writeJson(displayPath, display);
  writePngHeader(join(dir, 'assets/ui/panel.png'), 1024, 100);
  const manifestPath = join(dir, 'assets/asset-manifest.json');
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  manifest.assets[0].targetWidth = 1024;
  writeJson(manifestPath, manifest);
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unscaled_native_asset|untouched-source guard/);

  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  delete config.gates.ui_scale.max_texture_px;
  writeJson(configPath, config);
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /gates\.ui_scale\.max_texture_px/);
  audit = JSON.parse(readFileSync(auditPath, 'utf8'));
  assert.equal(audit.schema_version, 2);
  assert.equal(audit.passed, false);
  assert.match(audit.config_errors[0], /max_texture_px/);
});

test('unknown UI roles fall back to key/category and thresholds validate bounds', (t) => {
  const dir = fixture(t);
  const configPath = join(dir, 'gca.config.json');
  writePngHeader(join(dir, 'assets/ui/panel.png'), 700, 100);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'results-surface',
      category: 'panel',
      path: 'assets/ui/panel.png',
    }],
  });
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 1000, h: 500 },
    ui: [{
      key: 'results-surface',
      role: 'custom surface',
      width: 700,
      height: 100,
      x: 500,
      y: 100,
    }],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    "export function draw(scene) { return scene.add.image(500, 100, 'results-surface'); }\n",
  );

  let result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);

  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  config.gates.ui_scale.aspect_tolerance = 0;
  config.gates.ui_scale.overlap_min_px = 0;
  writeJson(configPath, config);
  result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);

  config.gates.ui_scale.max_canvas_fraction.panel = 55;
  writeJson(configPath, config);
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /max_canvas_fraction\.panel/);
});

test('authored toolbar, compound HUD, and panel roles retain authority', (t) => {
  const dir = fixture(t);
  const rows = [
    {
      key: 'ui-tools',
      role: 'toolbar',
      width: 500,
      height: 40,
      y: 50,
      bucket: 'hud',
    },
    {
      key: 'ui-status-surface',
      role: 'health bar background',
      width: 400,
      height: 30,
      y: 125,
      bucket: 'hud',
    },
    {
      key: 'ui-health-shell',
      role: 'panel',
      width: 700,
      height: 40,
      y: 210,
      bucket: 'panel',
    },
  ];
  for (const row of rows) {
    writePngHeader(
      join(dir, `assets/ui/${row.key}.png`),
      row.width,
      row.height,
    );
  }
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: rows.map((row) => ({
      key: row.key,
      category: 'ui',
      path: `assets/ui/${row.key}.png`,
      width: row.width,
      height: row.height,
      targetWidth: row.width,
    })),
  });
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1000, designHeight: 500 },
    scenes: {
      GameScene: {
        elements: rows.map((row) => ({
          key: row.key,
          role: row.role,
          kind: 'static',
          box: [0.1, (row.y - row.height / 2) / 500, row.width / 1000, row.height / 500],
        })),
      },
    },
  });
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 1000, h: 500 },
    ui: rows.map((row) => ({
      key: row.key,
      role: row.role,
      width: row.width,
      height: row.height,
      x: 500,
      y: row.y,
    })),
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    rows.map(
      (row) => `scene.add.image(500, ${row.y}, '${row.key}');`,
    ).join('\n'),
  );

  const result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);
  const audit = JSON.parse(readFileSync(
    join(dir, '.playtest/verify_middle/ui-scale/ui-scale-report.json'),
    'utf8',
  ));
  for (const row of rows) {
    assert.equal(
      audit.elements.find((entry) => entry.key === row.key)?.bucket,
      row.bucket,
    );
  }
});

test('decorative frame and title-text roles retain banner exemptions', (t) => {
  const dir = fixture(t);
  const rows = [
    {
      key: 'ornate-shell',
      category: 'frame',
      role: 'frame',
      width: 900,
      height: 420,
      x: 500,
      y: 250,
    },
    {
      key: 'title-logo',
      category: 'logo',
      role: 'title text',
      width: 700,
      height: 80,
      x: 500,
      y: 70,
    },
  ];
  for (const row of rows) {
    writePngHeader(
      join(dir, `assets/ui/${row.key}.png`),
      row.width,
      row.height,
    );
  }
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: rows.map((row) => ({
      key: row.key,
      category: row.category,
      path: `assets/ui/${row.key}.png`,
      width: row.width,
      height: row.height,
      targetWidth: row.width,
      decorative: true,
    })),
  });
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1000, designHeight: 500 },
    scenes: {
      GameScene: {
        elements: rows.map((row) => ({
          key: row.key,
          role: row.role,
          kind: 'static',
          box: [
            (row.x - row.width / 2) / 1000,
            (row.y - row.height / 2) / 500,
            row.width / 1000,
            row.height / 500,
          ],
        })),
      },
    },
  });
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 1000, h: 500 },
    ui: rows.map((row) => ({
      key: row.key,
      role: row.role,
      width: row.width,
      height: row.height,
      x: row.x,
      y: row.y,
      decorative: true,
    })),
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    rows.map(
      (row) => `scene.add.image(${row.x}, ${row.y}, '${row.key}');`,
    ).join('\n'),
  );

  const result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);
  const audit = JSON.parse(readFileSync(
    join(dir, '.playtest/verify_middle/ui-scale/ui-scale-report.json'),
    'utf8',
  ));
  for (const row of rows) {
    assert.equal(
      audit.elements.find((entry) => entry.key === row.key)?.bucket,
      'banner',
    );
  }
});

test('cluster children do not inherit the container role', (t) => {
  const dir = fixture(t);
  writePngHeader(join(dir, 'assets/ui/health.png'), 200, 20);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-health-bar',
      category: 'ui',
      path: 'assets/ui/health.png',
      width: 200,
      height: 20,
      maxBox: [240, 40],
    }],
  });
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1000, designHeight: 500 },
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-mixed-cluster',
          role: 'banner',
          kind: 'cluster',
          box: [0.2, 0.1, 0.6, 0.2],
          children: [
            { key: 'ui-health-bar', kind: 'image' },
            { key: 'ui-title-logo', kind: 'image' },
          ],
        }],
      },
    },
  });
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 1000, h: 500 },
    ui: [
      {
        key: 'ui-mixed-cluster',
        role: 'banner',
        width: 600,
        height: 100,
        x: 500,
        y: 100,
      },
      {
        key: 'ui-health-bar',
        width: 200,
        height: 20,
        x: 350,
        y: 100,
      },
      {
        key: 'ui-title-logo',
        width: 180,
        height: 40,
        x: 650,
        y: 100,
      },
    ],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    "scene.add.container(500, 100).setName('ui-mixed-cluster');\n"
      + "scene.add.image(350, 100, 'ui-health-bar');\n"
      + "scene.add.image(650, 100, 'ui-title-logo');\n",
  );

  const result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);
  const audit = JSON.parse(readFileSync(
    join(dir, '.playtest/verify_middle/ui-scale/ui-scale-report.json'),
    'utf8',
  ));
  assert.equal(
    audit.elements.find((entry) => entry.key === 'ui-health-bar')?.bucket,
    'hud',
  );
});

test('authored banner role cannot weaken functional HUD semantics', (t) => {
  const dir = fixture(t);
  writePngHeader(join(dir, 'assets/ui/health.png'), 700, 20);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-health-bar',
      category: 'ui',
      path: 'assets/ui/health.png',
      targetWidth: 700,
    }],
  });
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    schema_version: 1,
    canvas: { designWidth: 1000, designHeight: 500 },
    waived_scenes: [],
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-health-bar',
          role: 'banner',
          kind: 'static',
          box: [0.1, 0.1, 0.7, 0.04],
          sourceStrategy: 'generated-image',
        }],
      },
    },
  });
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 1000, h: 500 },
    ui: [{
      key: 'ui-health-bar',
      role: 'banner',
      width: 700,
      height: 20,
      x: 450,
      y: 60,
    }],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src/Scene.js'),
    "export function draw(scene) { return scene.add.image(450, 60, 'ui-health-bar'); }\n",
  );

  const result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /conflicts with key\/category semantics 'hud'/);
});

test('all canonical UI categories require display ledger coverage', (t) => {
  for (const category of ['dashboard', 'toolbar', 'counter']) {
    const dir = fixture(t);
    writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
    writeJson(join(dir, 'assets/asset-manifest.json'), {
      assets: [{
        key: `${category}-main`,
        category,
        path: `assets/ui/${category}.png`,
        targetWidth: 120,
      }],
    });
    const result = runGate(dir, 'ui_contract');
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, new RegExp(`${category}-main`));
  }
});

test('runtime composition bindings are enforced from layout contract', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-timer-panel',
          kind: 'widget',
          sourceStrategy: 'composition',
          anchors: [{
            id: 'timer',
            binds: 'timer',
            xPct: 0.5,
            yPct: 0.5,
          }],
        }],
      },
    },
  });
  writeJson(join(dir, '.playtest/inputs/display-params.json'), {
    uiCanvas: { w: 320, h: 180 },
    ui: [{ key: 'ui-timer-panel', width: 100, height: 40, x: 60, y: 30 }],
  });
  mkdirSync(join(dir, 'src'), { recursive: true });
  const scenePath = join(dir, 'src/Scene.js');
  writeFileSync(
    scenePath,
    "export function draw(scene, timer) { const panel = scene.add.container(); panel.name = 'ui-timer-panel'; scene.add.text(0, 0, timer); }\n",
  );
  let result = runGate(dir, 'ui_contract');
  assert.equal(result.status, 0, result.stderr);

  writeFileSync(
    scenePath,
    "export function draw(scene) { const panel = scene.add.container(); panel.name = 'ui-timer-panel'; }\n",
  );
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /no matching live text binding/);

  writeFileSync(
    scenePath,
    "export function draw(scene) { const hostKey = 'ui-timer-panel'; return scene.add.rectangle(0, 0, 10, 10); }\n",
  );
  result = runGate(dir, 'ui_contract');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /binding visual is never instantiated\/named/);
  assert.match(result.stderr, /no matching live text binding/);
});

test('formal test gate requires a non-empty passing suite', (t) => {
  const dir = fixture(t);
  const resultPath = join(dir, '.playtest/verify/test-run/test-results.json');
  const testsPath = join(dir, '.playtest/tests');
  writeJson(join(dir, '.playtest/state.json'), {
    tests: { failing: 2, stale: 1 },
  });

  writeJson(resultPath, formalTestResult({ testsPath }));
  const passing = runGate(dir, 'tests_passing');
  assert.equal(passing.status, 0, passing.stderr);
  assert.match(passing.stderr, /canonical formal results passed/);

  writeJson(resultPath, formalTestResult({ total: 0, pass: 0, returncode: 1, testsPath }));
  const empty = runGate(dir, 'tests_passing');
  assert.equal(empty.status, 1);
  assert.match(empty.stderr, /summary\.total > 0/);
});

test('formal test gate rejects solo evidence and failed tests', (t) => {
  const dir = fixture(t);
  const resultPath = join(dir, '.playtest/verify/test-run/test-results.json');
  const testsPath = join(dir, '.playtest/tests');

  writeJson(resultPath, formalTestResult({
    mode: 'solo',
    fullSuite: false,
    testsPath,
  }));
  const solo = runGate(dir, 'tests_passing');
  assert.equal(solo.status, 1);
  assert.match(solo.stderr, /solo\/scoped evidence/);

  writeJson(resultPath, formalTestResult({
    fullSuite: false,
    selectors: { changed: 'src/GameScene.js' },
    testsPath,
  }));
  const scoped = runGate(dir, 'tests_passing');
  assert.equal(scoped.status, 1);
  assert.match(scoped.stderr, /not a full unfiltered/);

  writeJson(resultPath, formalTestResult({
    fullSuite: false,
    selectors: {},
    testsPath: join(testsPath, 'smoke'),
  }));
  const noncanonicalPath = runGate(dir, 'tests_passing');
  assert.equal(noncanonicalPath.status, 1);
  assert.match(noncanonicalPath.stderr, /not a full unfiltered/);

  writeJson(resultPath, formalTestResult({
    total: 2,
    pass: 1,
    fail: 1,
    testsPath,
  }));
  const failed = runGate(dir, 'tests_passing');
  assert.equal(failed.status, 1);
  assert.match(failed.stderr, /1 formal test\(s\) failing/);
});

test('physics alignment cannot claim N/A when routing requires evidence', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  writeRunIdentity(dir, { interactionProfile: 'matter' });
  const alignmentPath = join(
    dir,
    '.playtest/verify/alignment/physics/alignment.json',
  );
  const writeNotApplicable = () => writeJson(alignmentPath, {
    axis: 'physics',
    applicable: false,
    aligned: null,
    checked: [],
    issues: [],
  });

  writeNotApplicable();
  let result = runGate(dir, 'iteration_ready');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cannot be applicable:false/);

  writeJson(alignmentPath, {
    axis: 'physics',
    applicable: true,
    aligned: true,
    checked: [],
    issues: [],
  });
  result = runGate(dir, 'iteration_ready');
  assert.doesNotMatch(result.stderr, /cannot be applicable:false/);
});

test('visual provider exhaustion warns by default without passing alignment', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  const runId = 'visual-unavailable-test';
  const alignment = {
    applicable: true,
    status: 'unavailable',
    aligned: null,
    source: 'element-visual-diff.mjs',
    run_id: runId,
    checked: [],
    issues: [],
  };
  writeJson(join(dir, '.playtest/verify/alignment/visual/alignment.json'), {
    axis: 'visual',
    ...alignment,
  });
  writeJson(join(dir, '.playtest/verify/alignment/element/alignment.json'), {
    axis: 'element',
    ...alignment,
  });
  writeJson(join(dir, '.playtest/verify_middle/visual/visual-diff-report.json'), {
    schema_version: 1,
    source: 'element-visual-diff.mjs',
    run_id: runId,
    applicable: true,
    status: 'unavailable',
    aligned: null,
    unavailable_count: 2,
    unavailable_policy: 'warn',
    reason: 'Doubao request unavailable',
    method: 'paired_vlm',
    reference: 'concept_crop',
    axes: { global: true, element: true },
  });

  const result = runGate(dir, 'visual_diff');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /continuing because unavailable_policy=warn/);
});

test('visual provider outage stays non-blocking even if stale config requests block', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'gca.config.json'), {
    gates: { visual: { enabled: true, unavailable_policy: 'block' } },
  });
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  const runId = 'visual-unavailable-block-test';
  for (const axis of ['visual', 'element']) {
    writeJson(join(dir, `.playtest/verify/alignment/${axis}/alignment.json`), {
      axis,
      applicable: true,
      status: 'unavailable',
      aligned: null,
      source: 'element-visual-diff.mjs',
      run_id: runId,
      checked: [],
      issues: [],
    });
  }
  writeJson(join(dir, '.playtest/verify_middle/visual/visual-diff-report.json'), {
    schema_version: 1,
    source: 'element-visual-diff.mjs',
    run_id: runId,
    applicable: true,
    status: 'unavailable',
    aligned: null,
    reason: 'Doubao request unavailable',
    method: 'paired_vlm',
    reference: 'concept_crop',
    axes: { global: true, element: true },
  });

  const result = runGate(dir, 'visual_diff');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /continuing because unavailable_policy=warn/);
});

test('visual gate rejects zero-comparison pass and unavailable reports hiding mismatch', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  const runId = 'visual-consistency-test';
  for (const axis of ['visual', 'element']) {
    writeJson(join(dir, `.playtest/verify/alignment/${axis}/alignment.json`), {
      axis,
      applicable: true,
      status: 'passed',
      aligned: true,
      source: 'element-visual-diff.mjs',
      run_id: runId,
      checked: [],
      issues: [],
    });
  }
  const sidecarPath = join(dir, '.playtest/verify_middle/visual/visual-diff-report.json');
  writeJson(sidecarPath, {
    schema_version: 1,
    source: 'element-visual-diff.mjs',
    run_id: runId,
    applicable: true,
    status: 'passed',
    aligned: true,
    method: 'paired_vlm',
    reference: 'concept_crop',
    axes: { global: true, element: true },
    coverage: {
      expected_scenes: 1,
      captured_scenes: 0,
      compared_global: 0,
      expected_elements: 0,
      compared_elements: 0,
    },
    scene_screen_map: { GameScene: 'gameplay' },
    scenes: [],
  });
  const empty = runGate(dir, 'visual_diff');
  assert.equal(empty.status, 1);
  assert.match(empty.stderr, /global coverage is incomplete/);

  for (const axis of ['visual', 'element']) {
    const alignmentPath = join(dir, `.playtest/verify/alignment/${axis}/alignment.json`);
    const alignment = JSON.parse(readFileSync(alignmentPath, 'utf8'));
    alignment.status = 'unavailable';
    alignment.aligned = null;
    writeJson(alignmentPath, alignment);
  }
  writeJson(sidecarPath, {
    schema_version: 1,
    source: 'element-visual-diff.mjs',
    run_id: runId,
    applicable: true,
    status: 'unavailable',
    aligned: null,
    reason: 'provider failed',
    method: 'paired_vlm',
    reference: 'concept_crop',
    axes: { global: true, element: true },
    scenes: [{
      scene: 'GameScene',
      global: { match: false, status: 'failed' },
      elements: [],
    }],
  });
  const hiddenMismatch = runGate(dir, 'visual_diff');
  assert.equal(hiddenMismatch.status, 1);
  assert.match(hiddenMismatch.stderr, /contains a real match:false verdict/);
});

test('visual gate rejects stale not-applicable evidence once mockups exist', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  const runId = 'visual-old-na-test';
  for (const axis of ['visual', 'element']) {
    writeJson(join(dir, `.playtest/verify/alignment/${axis}/alignment.json`), {
      axis,
      applicable: false,
      status: 'not_applicable',
      aligned: null,
      source: 'element-visual-diff.mjs',
      run_id: runId,
    });
  }
  writeJson(join(dir, '.playtest/verify_middle/visual/visual-diff-report.json'), {
    source: 'element-visual-diff.mjs',
    run_id: runId,
    applicable: false,
    status: 'not_applicable',
    aligned: null,
    axes: { global: true, element: true },
  });

  const result = runGate(dir, 'visual_diff');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /not applicable even though concept mockups/);
});

test('warning-only visual outage does not consume or block polish rounds', (t) => {
  const dir = fixture(t);
  const historyPath = join(dir, '.playtest/verify/play/agent-play-history.jsonl');
  mkdirSync(dirname(historyPath), { recursive: true });
  writeFileSync(historyPath, `${JSON.stringify(playHistoryEntry())}\n`);
  const sidecarPath = join(dir, '.playtest/verify_middle/visual/visual-diff-report.json');
  writeJson(sidecarPath, {
    status: 'unavailable',
    aligned: null,
  });

  const unavailable = runGate(dir, 'polish_rounds');
  assert.equal(unavailable.status, 0, unavailable.stderr);
  assert.match(unavailable.stderr, /waived because the VLM is unavailable/);

  writeJson(sidecarPath, {
    status: 'passed',
    aligned: true,
    round: 0,
  });
  const available = runGate(dir, 'polish_rounds');
  assert.equal(available.status, 1);
  assert.match(available.stderr, /visual evidence rounds = 0/);

  writeJson(sidecarPath, {
    status: 'passed',
    aligned: true,
    round: 1,
  });
  const evidenced = runGate(dir, 'polish_rounds');
  assert.equal(evidenced.status, 0, evidenced.stderr);
});

test('polish gate derives rounds and stress from canonical evidence', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'gca.config.json'), {
    gates: {
      polish: {
        visual_rounds_min: 1,
        gameplay_rounds_min: 1,
        require_stress_tested: true,
      },
    },
  });
  writeJson(
    join(dir, '.playtest/verify_middle/visual/visual-diff-report.json'),
    { status: 'passed', aligned: true, round: 1 },
  );
  const historyPath = join(dir, '.playtest/verify/play/agent-play-history.jsonl');
  mkdirSync(dirname(historyPath), { recursive: true });
  writeFileSync(historyPath, `${JSON.stringify(playHistoryEntry())}\n`);
  const testsPath = join(dir, '.playtest/tests');
  const formal = formalTestResult({ testsPath });
  formal.result.tests[0].name = 'stress input soak';
  writeJson(
    join(dir, '.playtest/verify/test-run/test-results.json'),
    formal,
  );

  const passing = runGate(dir, 'polish_rounds');
  assert.equal(passing.status, 0, passing.stderr);

  formal.result.tests[0].status = 'FAIL';
  writeJson(
    join(dir, '.playtest/verify/test-run/test-results.json'),
    formal,
  );
  const failedStress = runGate(dir, 'polish_rounds');
  assert.equal(failedStress.status, 1);
  assert.match(failedStress.stderr, /no passing formal stress\/soak test/);

  formal.result.tests[0].status = 'PASS';
  formal.result.tests[0].name = 'regular smoke';
  writeJson(
    join(dir, '.playtest/verify/test-run/test-results.json'),
    formal,
  );
  const missingStress = runGate(dir, 'polish_rounds');
  assert.equal(missingStress.status, 1);
  assert.match(missingStress.stderr, /no passing formal stress\/soak test/);
});

test('polish gate validates producer histories instead of trusting injected rounds', (t) => {
  const dir = fixture(t);
  const visualDir = join(dir, '.playtest/verify_middle/visual');
  const sidecarPath = join(visualDir, 'visual-diff-report.json');
  const playHistoryPath = join(dir, '.playtest/verify/play/agent-play-history.jsonl');
  mkdirSync(dirname(playHistoryPath), { recursive: true });
  writeFileSync(playHistoryPath, `${JSON.stringify(playHistoryEntry())}\n`);

  writeJson(sidecarPath, {
    schema_version: 2,
    source: 'element-visual-diff.mjs',
    run_id: 'visual-current',
    status: 'passed',
    aligned: true,
    round: 999,
  });
  const injected = runGate(dir, 'polish_rounds');
  assert.equal(injected.status, 1);
  assert.match(injected.stderr, /visual evidence rounds = 0/);

  writeFileSync(
    join(visualDir, 'visual-diff-history.jsonl'),
    `${JSON.stringify({
      schema_version: 1,
      source: 'element-visual-diff.mjs',
      run_id: 'visual-current',
      generated_at: '2026-07-12T00:00:00.000Z',
      status: 'passed',
      aligned: true,
      coverage: { compared_global: 1, compared_elements: 0 },
    })}\n`,
  );
  const evidenced = runGate(dir, 'polish_rounds');
  assert.equal(evidenced.status, 0, evidenced.stderr);

  writeJson(sidecarPath, {
    schema_version: 1,
    status: 'passed',
    aligned: true,
    round: 999,
  });
  const inflatedLegacy = runGate(dir, 'polish_rounds');
  assert.equal(inflatedLegacy.status, 1);
  assert.match(inflatedLegacy.stderr, /visual evidence rounds = 0/);
});

test('polish gate ignores malformed and unsuccessful gameplay history', (t) => {
  const dir = fixture(t);
  writeJson(
    join(dir, '.playtest/verify_middle/visual/visual-diff-report.json'),
    { schema_version: 1, status: 'passed', aligned: true, round: 1 },
  );
  const historyPath = join(dir, '.playtest/verify/play/agent-play-history.jsonl');
  mkdirSync(dirname(historyPath), { recursive: true });
  writeFileSync(
    historyPath,
    `not-json\n${JSON.stringify(playHistoryEntry({ build_health: false }))}\n`,
  );

  const result = runGate(dir, 'polish_rounds');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /gameplay evidence rounds = 0/);
});

test('polish gate resolves legacy gameplay history independently of play report', (t) => {
  const dir = fixture(t);
  writeJson(
    join(dir, '.playtest/verify_middle/visual/visual-diff-report.json'),
    { schema_version: 1, status: 'passed', aligned: true, round: 1 },
  );
  writeJson(
    join(dir, '.playtest/verify/play/agent-play-report.json'),
    { generated: '2026-07-12T00:00:00.000Z' },
  );
  const legacyHistory = join(dir, '.playtest/play/agent-play-history.jsonl');
  mkdirSync(dirname(legacyHistory), { recursive: true });
  writeFileSync(legacyHistory, `${JSON.stringify(playHistoryEntry())}\n`);

  const result = runGate(dir, 'polish_rounds');
  assert.equal(result.status, 0, result.stderr);
});

test('verify report allows warning-only partial but rejects blocking partial/blocked', (t) => {
  const dir = fixture(t);
  let identity = writeRunIdentity(dir, { status: 'partial' });
  writeVerifyReport(dir, identity, {
    issues: [{ severity: 'warning', description: 'VLM unavailable' }],
  });
  const partial = runGate(dir, 'verify_report');
  assert.equal(partial.status, 0, partial.stderr);

  identity = writeRunIdentity(dir, { status: 'partial' });
  writeVerifyReport(dir, identity, {
    issues: [{ severity: 'high', description: 'producer failed' }],
  });
  const blockingPartial = runGate(dir, 'verify_report');
  assert.equal(blockingPartial.status, 1);
  assert.match(blockingPartial.stderr, /partial with incomplete\/blocking evidence/);

  identity = writeRunIdentity(dir, { status: 'blocked' });
  writeVerifyReport(dir, identity, {
    issues: [{ severity: 'high', description: 'layout failed' }],
  });
  const blocked = runGate(dir, 'verify_report');
  assert.equal(blocked.status, 1);
  assert.match(blocked.stderr, /status is blocked/);
});

test('verify epoch gate rejects active, report-tampered, and forged runtime evidence', (t) => {
  const dir = fixture(t);
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  let result = runGate(dir, 'verify_report');
  assert.equal(result.status, 0, result.stderr);

  const runPath = join(dir, '.playtest/verify/current-run.json');
  const current = JSON.parse(readFileSync(runPath, 'utf8'));
  current.status = 'active';
  writeJson(runPath, current);
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not finalize and hash/);

  current.status = 'finalized';
  writeJson(runPath, current);
  report.issues.push({ severity: 'warning', description: 'post-finalize edit' });
  writeJson(join(dir, '.playtest/verify/verify-report.json'), report);
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not finalize and hash/);

  const refreshed = finalizeVerifyReport(dir, {
    ...report,
    issues: [],
  });
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  registry.runtime_identity.page_url = 'http://localhost:9999/';
  writeJson(registryPath, registry);
  refreshed.runtime_asset_registry = registry;
  refreshed.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, refreshed);
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /runtime_identity is stale\/cross-run/);
});

test('full verify gate seals the complete video receipt chain', (t) => {
  const dir = fixture(t);
  const identity = writeRunIdentity(dir, { mode: 'full' });
  const report = writeVerifyReport(dir, identity);
  let result = runGate(dir, 'verify_report');
  assert.equal(result.status, 0, result.stderr);

  writeFileSync(
    join(dir, '.playtest/verify/recording/core-loop/recording.webm'),
    'tampered video',
  );
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /changed after verify-report finalization/);

  finalizeVerifyReport(dir, report);
  const analysisPath = join(
    dir,
    '.playtest/verify/recording/core-loop/video-analysis.json',
  );
  const analysis = JSON.parse(readFileSync(analysisPath, 'utf8'));
  analysis.video_receipt_sha256 = '0'.repeat(64);
  writeJson(analysisPath, analysis);
  const resealed = JSON.parse(
    readFileSync(join(dir, '.playtest/verify/verify-report.json'), 'utf8'),
  );
  resealed.artifacts.video_analyses = [
    artifactEntry(
      dir,
      '.playtest/verify/recording/core-loop/video-analysis.json',
    ),
  ];
  resealVerifyReport(dir, resealed);
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /video analysis is not bound/);
});

test('runtime asset gate recomputes each manifest loaded and used row', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{ key: 'player', textureKey: 'player-texture' }],
  });
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  registry.manifest_assets[0].used = false;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);
  const result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /forged\/stale registry row/);
});

test('runtime asset gate validates normalized nested manifest rows', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    backgrounds: {
      sky: {
        sourceStrategy: 'generated-image',
        description: 'nested sky',
        path: 'assets/backgrounds/sky.png',
      },
    },
  });
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  registry.loaded_texture_keys = ['sky'];
  registry.animation_frame_texture_keys = ['sky'];
  registry.manifest_assets = [{
    key: 'sky',
    declared_texture_keys: ['sky'],
    loaded_texture_keys: ['sky'],
    animation_frame_texture_keys: ['sky'],
    display_list_texture_keys: [],
    loaded: true,
    used: true,
  }];
  registry.summary.loaded_texture_count = 1;
  registry.summary.animation_texture_count = 1;
  registry.summary.manifest_asset_count = 1;
  registry.summary.manifest_loaded_count = 1;
  registry.summary.manifest_used_count = 1;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);

  const result = runGate(dir, 'verify_report');
  assert.equal(result.status, 0, result.stderr);
});

test('runtime asset gate enforces each per-action texture contract', (t) => {
  const dir = fixture(t);
  const player = spriteManifestEntry('player', ['idle', 'pose']);
  writeJson(join(dir, player.actions.idle.metadata), {
    frameWidth: 32,
    frameHeight: 32,
    frameCount: 4,
    fps: 12,
  });
  writeJson(join(dir, player.actions.pose.metadata), {
    frameWidth: 32,
    frameHeight: 32,
    frameCount: 1,
    fps: 1,
  });
  writeJson(join(dir, 'assets/asset-manifest.json'), { assets: [player] });
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));

  registry.animation_frame_texture_keys = registry.animation_frame_texture_keys
    .filter((key) => key !== 'player-pose');
  registry.manifest_assets[0].animation_frame_texture_keys = registry
    .manifest_assets[0].animation_frame_texture_keys
    .filter((key) => key !== 'player-pose');
  registry.summary.animation_texture_count = registry.animation_frame_texture_keys.length;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);

  let result = runGate(dir, 'verify_report');
  assert.equal(result.status, 0, result.stderr);

  registry.animation_frame_texture_keys = registry.animation_frame_texture_keys
    .filter((key) => key !== 'player-idle');
  registry.manifest_assets[0].animation_frame_texture_keys = registry
    .manifest_assets[0].animation_frame_texture_keys
    .filter((key) => key !== 'player-idle');
  registry.manifest_assets[0].used = false;
  registry.summary.animation_texture_count = registry.animation_frame_texture_keys.length;
  registry.summary.manifest_used_count = 0;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(
    result.stderr,
    /animated action 'idle' texture 'player-idle' does not participate/,
  );

  registry.animation_frame_texture_keys.push('player-idle');
  registry.animation_frame_texture_keys.sort();
  registry.manifest_assets[0].animation_frame_texture_keys.push('player-idle');
  registry.manifest_assets[0].animation_frame_texture_keys.sort();
  registry.manifest_assets[0].used = true;
  registry.summary.animation_texture_count = registry.animation_frame_texture_keys.length;
  registry.summary.manifest_used_count = 1;
  registry.loaded_texture_keys = registry.loaded_texture_keys
    .filter((key) => key !== 'player-pose');
  registry.manifest_assets[0].loaded_texture_keys = registry
    .manifest_assets[0].loaded_texture_keys
    .filter((key) => key !== 'player-pose');
  registry.manifest_assets[0].loaded = false;
  registry.manifest_assets[0].used = false;
  registry.summary.loaded_texture_count = registry.loaded_texture_keys.length;
  registry.summary.manifest_loaded_count = 0;
  registry.summary.manifest_used_count = 0;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);

  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /action 'pose' texture 'player-pose' was not loaded/);
});

test('runtime asset gate rejects loaded or displayed undeclared textures', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{ key: 'player', textureKey: 'player-texture' }],
  });
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  registry.loaded_texture_keys.push('undeclared-texture');
  registry.loaded_texture_keys.sort();
  registry.display_list_texture_keys.push('undeclared-texture');
  registry.display_list_texture_keys.sort();
  registry.summary.loaded_texture_count = registry.loaded_texture_keys.length;
  registry.summary.display_texture_count = registry.display_list_texture_keys.length;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);

  const result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /undeclared texture key\(s\): undeclared-texture/);
});

test('runtime asset gate allows Phaser-owned UUID textures for declared runtime UI', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    runtimeSources: ['composition', 'live-text'],
    assets: [{ key: 'player', textureKey: 'player-texture' }],
  });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'hud',
          kind: 'cluster',
          sourceStrategy: 'composition',
          children: [{
            kind: 'text',
            bind: 'score',
            sourceStrategy: 'live-text',
          }],
        }],
      },
    },
  });
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  const textTexture = '12345678-1234-4abc-8def-1234567890ab';
  registry.loaded_texture_keys.push(textTexture);
  registry.loaded_texture_keys.sort();
  registry.display_list_texture_keys.push(textTexture);
  registry.display_list_texture_keys.sort();
  registry.scenes[0].texture_keys.push(textTexture);
  registry.scenes[0].texture_keys.sort();
  const binding = {
    scene: 'GameScene',
    texture_key: textTexture,
    object_type: 'Text',
    name: 'score',
    layout_key: 'score',
    bind: 'score',
    instance: 0,
    visible: true,
  };
  registry.scenes[0].texture_bindings = [binding];
  registry.display_object_texture_bindings = [binding];
  registry.summary.loaded_texture_count = registry.loaded_texture_keys.length;
  registry.summary.display_texture_count = registry.display_list_texture_keys.length;
  registry.summary.display_object_texture_binding_count = 1;
  registry.summary.runtime_uuid_texture_count = 1;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);

  let result = runGate(dir, 'verify_report');
  assert.equal(result.status, 0, result.stderr);

  registry.summary.display_object_texture_binding_count = 0;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /summary\.display_object_texture_binding_count/);
});

test('runtime UUID text binds to its file-backed host and exact manifest anchor', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-timer-panel',
      category: 'ui',
      sourceStrategy: 'generated-image',
      path: 'assets/ui/timer-panel.png',
      anchors: [{
        id: 'timer-value',
        binds: 'elapsedSeconds',
        xPct: 0.5,
        yPct: 0.5,
      }],
    }],
  });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-timer-panel',
          sourceStrategy: 'generated-image',
        }],
      },
    },
  });
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  const textTexture = '12345678-1234-4abc-8def-1234567890ab';
  const binding = {
    scene: 'GameScene',
    texture_key: textTexture,
    object_type: 'Text',
    name: 'timer-value',
    layout_key: 'ui-timer-panel',
    bind: 'elapsedSeconds',
    instance: 0,
    visible: true,
  };
  registry.loaded_texture_keys.push(textTexture);
  registry.loaded_texture_keys.sort();
  registry.display_list_texture_keys.push(textTexture);
  registry.display_list_texture_keys.sort();
  registry.scenes[0].texture_keys = [textTexture];
  registry.scenes[0].texture_bindings = [binding];
  registry.display_object_texture_bindings = [binding];
  registry.summary.loaded_texture_count = registry.loaded_texture_keys.length;
  registry.summary.display_texture_count = 1;
  registry.summary.display_object_texture_binding_count = 1;
  registry.summary.runtime_uuid_texture_count = 1;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);

  let result = runGate(dir, 'verify_report');
  assert.equal(result.status, 0, result.stderr);

  const forged = { ...binding, bind: 'unrelatedValue' };
  registry.scenes[0].texture_bindings = [forged];
  registry.display_object_texture_bindings = [forged];
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /manifest-declared file-backed text anchor/);
});

test('runtime asset gate rejects unrelated UUID display bindings', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    runtimeSources: ['live-text'],
    assets: [],
  });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'score',
          bind: 'score',
          sourceStrategy: 'live-text',
        }],
      },
    },
  });
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  const textTexture = '12345678-1234-4abc-8def-1234567890ab';
  const binding = {
    scene: 'GameScene',
    texture_key: textTexture,
    object_type: 'Text',
    name: 'unrelated-label',
    layout_key: 'unrelated-label',
    bind: null,
    instance: 0,
    visible: true,
  };
  registry.loaded_texture_keys.push(textTexture);
  registry.loaded_texture_keys.sort();
  registry.display_list_texture_keys.push(textTexture);
  registry.display_list_texture_keys.sort();
  registry.scenes[0].texture_keys = [textTexture];
  registry.scenes[0].texture_bindings = [binding];
  registry.display_object_texture_bindings = [binding];
  registry.summary.loaded_texture_count = registry.loaded_texture_keys.length;
  registry.summary.display_texture_count = 1;
  registry.summary.display_object_texture_binding_count = 1;
  registry.summary.runtime_uuid_texture_count = 1;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);

  const result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /not bound to a recursively declared runtime layout node/);
});

test('runtime asset gate rejects loaded-only UUID textures', (t) => {
  const dir = fixture(t);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    runtimeSources: ['live-text'],
    assets: [],
  });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    scenes: {
      GameScene: {
        elements: [{
          key: 'score',
          bind: 'score',
          sourceStrategy: 'live-text',
        }],
      },
    },
  });
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  const textTexture = '12345678-1234-4abc-8def-1234567890ab';
  registry.loaded_texture_keys.push(textTexture);
  registry.loaded_texture_keys.sort();
  registry.summary.loaded_texture_count = registry.loaded_texture_keys.length;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);

  const result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /loaded-only; UUID textures must be actually displayed/);
});

test('runtime asset gate rejects registered scenes that cannot be reached', (t) => {
  const dir = fixture(t);
  const identity = writeRunIdentity(dir);
  const report = writeVerifyReport(dir, identity);
  const registryPath = join(dir, '.playtest/verify/runtime/runtime-assets.json');
  const registry = JSON.parse(readFileSync(registryPath, 'utf8'));
  registry.registered_scene_keys = ['GameScene', 'MenuScene'];
  registry.scenes.push({
    scene: 'MenuScene',
    reachable: false,
    active: false,
    visible: false,
    error: 'scene failed to start',
    texture_keys: [],
  });
  registry.summary.registered_scene_count = 2;
  writeJson(registryPath, registry);
  report.runtime_asset_registry = registry;
  report.artifacts.runtime_asset_registry = artifactEntry(
    dir,
    '.playtest/verify/runtime/runtime-assets.json',
  );
  resealVerifyReport(dir, report);
  const result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /registered scene\(s\) that cannot be started\/reached/);
});

test('verify report rejects cross-run or stale build identity', (t) => {
  const dir = fixture(t);
  const identity = writeRunIdentity(dir);
  writeVerifyReport(dir, identity, {
    run_id: 'verify-old',
    verify_run_id: 'verify-old',
  });
  let result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /verify_run_id is missing\/cross-run/);

  writeFileSync(join(dir, 'index.html'), '<script>changed</script>\n');
  result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /build_digest is stale/);
});

test('verify report rejects split run_id and verify_run_id', (t) => {
  const dir = fixture(t);
  const identity = writeRunIdentity(dir);
  writeVerifyReport(dir, identity, {
    run_id: 'verify-other',
  });
  const result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /run_id must equal verify_run_id/);
});

test('verify report rejects routing metadata drift within an epoch', (t) => {
  const dir = fixture(t);
  const identity = writeRunIdentity(dir);
  writeVerifyReport(dir, identity);
  const currentPath = join(dir, '.playtest/verify/current-run.json');
  const current = JSON.parse(readFileSync(currentPath, 'utf8'));
  current.quality_profile = 'minimal';
  writeJson(currentPath, current);
  const result = runGate(dir, 'verify_report');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /quality_profile is missing\/cross-run/);
});

test('composite gates enforce iterate versus final verify mode', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  let identity = writeRunIdentity(dir, { mode: 'full' });
  const writeReport = (mode) => writeVerifyReport(dir, identity, { mode });
  writeReport('full');
  let result = runGate(dir, 'iteration_ready');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /invalid for this gate \(expected iterate\)/);

  identity = writeRunIdentity(dir, { mode: 'iterate' });
  writeReport('iterate');
  result = runGate(dir, 'ready_to_ship');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /invalid for this gate \(expected full or full-novideo\)/);
  result = runGate(dir, 'step_done', { step: 7 });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /invalid for this gate \(expected full or full-novideo\)/);
});

test('skip-video release requires a build-bound waiver', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  writeJson(join(dir, 'gca.config.json'), {
    verify: { skip_video: true },
    quality: { default_profile: 'art_directed' },
  });
  const identity = writeRunIdentity(dir, { mode: 'full' });
  let result = runGate(dir, 'ready_to_ship');
  assert.match(result.stderr, /requires an explicit state\.release_waivers/);
  result = runGate(dir, 'step_done', { step: 7 });
  assert.match(result.stderr, /requires an explicit state\.release_waivers/);
  const waiverFailure = JSON.parse(result.stdout).failures.find(
    (failure) => failure.check === 'release_waivers',
  );
  assert.equal(waiverFailure.agent, 'orchestrator');

  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.release_waivers = [{
    id: 'video-performance',
    reason: 'Operator accepts missing video performance evidence',
    approved_by: 'operator',
    approved_at: '2026-07-12T00:00:00Z',
    approval_event_id: 'waiver-test-approval',
    pipeline_run_id: identity.pipelineRunId,
    build_digest: identity.buildDigest,
  }];
  writeJson(statePath, state);
  result = runGate(dir, 'ready_to_ship');
  assert.doesNotMatch(result.stderr, /requires an explicit state\.release_waivers/);
  result = runGate(dir, 'step_done', { step: 7 });
  assert.doesNotMatch(result.stderr, /requires an explicit state\.release_waivers/);
});

test('critic gate binds a consistent PASS to the latest verify run', (t) => {
  const dir = fixture(t);
  const verifyPath = join(dir, '.playtest/verify/verify-report.json');
  const criticPath = join(dir, '.playtest/verify/critic/critic-report.json');
  writeJson(verifyPath, {
    schema_version: 3,
    run_id: 'verify-current',
    pipeline_run_id: 'pipeline-current',
    build_digest: 'sha256:current',
    status: 'done',
    summary: { missing_artifacts: [] },
  });
  writeJson(criticPath, {
    schema_version: 2,
    source: 'critic-agent',
    verify_run_id: 'verify-current',
    pipeline_run_id: 'pipeline-current',
    build_digest: 'sha256:current',
    verdict: 'PASS',
    scores: {
      visual_quality: 0.8,
      gameplay_mechanics: 0.8,
      completeness: 0.8,
      polish: 0.8,
      asset_fitness: 0.8,
    },
    blocking_issues: [],
  });

  const passing = runGate(dir, 'critic');
  assert.equal(passing.status, 0, passing.stderr);

  const report = JSON.parse(readFileSync(criticPath, 'utf8'));
  report.verify_run_id = 'verify-old';
  writeJson(criticPath, report);
  const stale = runGate(dir, 'critic');
  assert.equal(stale.status, 1);
  assert.match(stale.stderr, /not bound to the latest pipeline\/build\/verify run/);

  report.verify_run_id = 'verify-current';
  report.scores.polish = 0.4;
  writeJson(criticPath, report);
  const inconsistent = runGate(dir, 'critic');
  assert.equal(inconsistent.status, 1);
  assert.match(inconsistent.stderr, /critic PASS is inconsistent/);
});

test('blocked layout gate still materializes failed alignment evidence', (t) => {
  const dir = fixture(t);
  const runId = 'layout-blocked-test';
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1280, designHeight: 720 },
    scenes: {
      GameScene: {
        elements: [
          { key: 'ui-start', role: 'button', kind: 'static', box: [0.4, 0.4, 0.2, 0.1] },
        ],
      },
    },
  });
  writeJson(join(dir, '.playtest/verify_middle/layout/GameScene.rendered.json'), {
    scene: 'GameScene',
    run_id: runId,
    canvas: { w: 1280, h: 720 },
    elements: [],
  });
  writeJson(join(dir, '.playtest/verify_middle/layout/_run.json'), {
    source: 'capture-layout.mjs',
    run_id: runId,
    scenes: ['GameScene'],
  });

  const result = runGate(dir, 'layout_alignment');
  assert.equal(result.status, 1);
  const evidence = JSON.parse(readFileSync(
    join(dir, '.playtest/verify/alignment/layout/alignment.json'),
    'utf8',
  ));
  assert.equal(evidence.aligned, false);
  assert.ok(evidence.issues.length > 0);
});

test('materialize layout alignment returns blocked evidence without failing the process', (t) => {
  const dir = fixture(t);
  const runId = 'layout-materialize-test';
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1280, designHeight: 720 },
    scenes: {
      GameScene: {
        elements: [
          { key: 'ui-start', role: 'button', kind: 'static', box: [0.4, 0.4, 0.2, 0.1] },
        ],
      },
    },
  });
  writeJson(join(dir, '.playtest/verify_middle/layout/GameScene.rendered.json'), {
    scene: 'GameScene',
    run_id: runId,
    canvas: { w: 1280, h: 720 },
    elements: [],
  });
  writeJson(join(dir, '.playtest/verify_middle/layout/_run.json'), {
    source: 'capture-layout.mjs',
    run_id: runId,
    scenes: ['GameScene'],
  });

  const result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(result.status, 0, result.stderr);
  const payload = JSON.parse(result.stdout);
  assert.equal(payload.gate, 'materialize_layout_alignment');
  assert.equal(payload.blocked, true);
  assert.equal(payload.aligned, false);
  assert.ok(payload.blocking_messages.length > 0);
  const evidence = JSON.parse(readFileSync(
    join(dir, '.playtest/verify/alignment/layout/alignment.json'),
    'utf8',
  ));
  assert.equal(evidence.source, 'layout-alignment-evaluator');
  assert.equal(evidence.aligned, false);
});

test('layout alignment skips only validated all-scene waivers', (t) => {
  const dir = fixture(t);
  const contractPath = join(dir, '.playtest/inputs/layout-contract.json');
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'minimal' });
  writeJson(contractPath, {
    scenes: {
      GameScene: { elements: [] },
    },
    waived_scenes: [],
  });

  let result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(result.status, 0, result.stderr);
  let payload = JSON.parse(result.stdout);
  assert.equal(payload.blocked, true);
  assert.match(payload.blocking_messages.join('\n'), /requires concept mockups/);

  writeJson(contractPath, {
    scenes: {
      GameScene: {
        reference: 'waived:concept-generation-unavailable',
        elements: [],
      },
    },
    waived_scenes: ['GameScene'],
  });
  result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(result.status, 0, result.stderr);
  payload = JSON.parse(result.stdout);
  assert.equal(payload.blocked, false);
  assert.equal(payload.applicable, false);
  assert.match(payload.reason, /validated concept-failure/);

  writeJson(contractPath, {
    scenes: {
      GameScene: { reference: 'waived:cost-cap', elements: [] },
    },
    waived_scenes: ['GameScene'],
  });
  result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(result.status, 0, result.stderr);
  payload = JSON.parse(result.stdout);
  assert.equal(payload.blocked, true);
  assert.match(payload.blocking_messages.join('\n'), /not an allowed exact waiver/);
});

test('rendered-bounds sizing follows native, responsive, and pixel profiles', (t) => {
  const dir = fixture(t);
  const runId = 'layout-profile-size';
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  writePngHeader(join(dir, 'assets/ui/panel.png'), 100, 50);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-panel',
      category: 'panel',
      path: 'assets/ui/panel.png',
      width: 100,
      height: 50,
      maxBox: [200, 100],
      baseSize: [100, 50],
      integerScales: [1, 2],
    }],
  });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1280, designHeight: 720 },
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-panel',
          role: 'hud panel',
          kind: 'static',
          box: [0.45, 0.45, 0.1, 0.1],
        }],
      },
    },
  });
  const writeCapture = (width, height) => {
    writeJson(join(dir, '.playtest/verify_middle/layout/GameScene.rendered.json'), {
      scene: 'GameScene',
      run_id: runId,
      canvas: { w: 1280, h: 720 },
      elements: [{
        name: 'ui-panel',
        layoutKey: 'ui-panel',
        type: 'Image',
        visible: true,
        cx: 640,
        cy: 360,
        w: width,
        h: height,
        effScrollFactorX: 0,
        effScrollFactorY: 0,
      }],
    });
    writeJson(join(dir, '.playtest/verify_middle/layout/_run.json'), {
      source: 'capture-layout.mjs',
      run_id: runId,
      scenes: ['GameScene'],
    });
  };

  writeCapture(200, 100);
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  let result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).blocked, false);

  const manifestPath = join(dir, 'assets/asset-manifest.json');
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  manifest.assets[0].maxBox = [100, 50];
  writeJson(manifestPath, manifest);
  writeCapture(200, 100);
  result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(JSON.parse(result.stdout).blocked, true);
  assert.match(result.stdout, /must fit manifest maxBox\/logicalBox/);
  manifest.assets[0].maxBox = [200, 100];
  writeJson(manifestPath, manifest);
  writeCapture(200, 100);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(JSON.parse(result.stdout).blocked, true);
  assert.match(result.stdout, /native 1:1/);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'pixel_art' });
  result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(JSON.parse(result.stdout).blocked, false);
  manifest.assets[0].baseSize = [99, 50];
  writeJson(manifestPath, manifest);
  writeCapture(200, 100);
  result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(JSON.parse(result.stdout).blocked, true);
  assert.match(result.stdout, /texture=baseSize/);
  manifest.assets[0].baseSize = [100, 50];
  writeJson(manifestPath, manifest);
  writeCapture(150, 75);
  result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(JSON.parse(result.stdout).blocked, true);
  assert.match(result.stdout, /positive integer uniform scale/);
});

test('materialize layout alignment reports write failures as blocked', (t) => {
  const dir = fixture(t);
  const alignmentParent = join(dir, '.playtest/verify/alignment');
  mkdirSync(alignmentParent, { recursive: true });
  writeFileSync(join(alignmentParent, 'layout'), 'blocks directory creation');

  const result = runGate(dir, 'materialize_layout_alignment');
  assert.equal(result.status, 0, result.stderr);
  const payload = JSON.parse(result.stdout);
  assert.equal(payload.wrote, false);
  assert.equal(payload.blocked, true);
  assert.match(payload.blocking_messages.join('\n'), /could not write/);
});

test('invalid layout contract writes fresh error alignment before blocking', (t) => {
  const dir = fixture(t);
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  const contractPath = join(dir, '.playtest/inputs/layout-contract.json');
  mkdirSync(dirname(contractPath), { recursive: true });
  writeFileSync(contractPath, '{invalid');

  const result = runGate(dir, 'layout_alignment');
  assert.equal(result.status, 1);
  const evidence = JSON.parse(readFileSync(
    join(dir, '.playtest/verify/alignment/layout/alignment.json'),
    'utf8',
  ));
  assert.equal(evidence.status, 'error');
  assert.equal(evidence.aligned, false);
  assert.match(evidence.issues[0], /invalid JSON/);
});

test('layout scene capture errors block even when the scene has no elements', (t) => {
  const dir = fixture(t);
  const runId = 'layout-error-test';
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1280, designHeight: 720 },
    scenes: { GameScene: { elements: [] } },
  });
  writeJson(join(dir, '.playtest/verify_middle/layout/GameScene.rendered.json'), {
    scene: 'GameScene',
    run_id: runId,
    error: 'scene not found',
    canvas: { w: 1280, h: 720 },
    elements: [],
  });
  writeJson(join(dir, '.playtest/verify_middle/layout/_run.json'), {
    source: 'capture-layout.mjs',
    run_id: runId,
    scenes: ['GameScene'],
  });

  const result = runGate(dir, 'layout_alignment');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /scene 'GameScene' not captured/);
  const evidence = JSON.parse(readFileSync(
    join(dir, '.playtest/verify/alignment/layout/alignment.json'),
    'utf8',
  ));
  assert.equal(evidence.status, 'failed');
  assert.match(evidence.issues.join('\n'), /capture failed/);
});

test('layout gate accepts one atomic run and rejects mixed-run scene evidence', (t) => {
  const dir = fixture(t);
  const runId = 'layout-atomic-test';
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  writePngHeader(join(dir, 'assets/ui/start.png'), 120, 50);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-start',
      category: 'ui',
      path: 'assets/ui/start.png',
      width: 120,
      height: 50,
    }],
  });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 1280, designHeight: 720 },
    scenes: {
      GameScene: {
        elements: [
          { key: 'ui-start', role: 'button', kind: 'static', box: [0.4, 0.4, 0.2, 0.1] },
        ],
      },
    },
  });
  const renderedPath = join(dir, '.playtest/verify_middle/layout/GameScene.rendered.json');
  writeJson(renderedPath, {
    scene: 'GameScene',
    run_id: runId,
    canvas: { w: 1280, h: 720 },
    elements: [{
      name: 'ui-start',
      layoutKey: 'ui-start',
      type: 'Image',
      cx: 640,
      cy: 324,
      w: 120,
      h: 50,
      visible: true,
      effScrollFactorX: 0,
      effScrollFactorY: 0,
    }],
  });
  writeJson(join(dir, '.playtest/verify_middle/layout/_run.json'), {
    source: 'capture-layout.mjs',
    run_id: runId,
    scenes: ['GameScene'],
  });

  const passing = runGate(dir, 'layout_alignment');
  assert.equal(passing.status, 0, passing.stderr);

  const mixed = JSON.parse(readFileSync(renderedPath, 'utf8'));
  mixed.run_id = 'old-run';
  writeJson(renderedPath, mixed);
  const rejected = runGate(dir, 'layout_alignment');
  assert.equal(rejected.status, 1);
  assert.match(rejected.stderr, /different\/legacy run/);
});

test('REAL-bounds layout sizing follows the quality profile', (t) => {
  const dir = fixture(t);
  const runId = 'layout-profile-sizing-test';
  mkdirSync(join(dir, 'design'), { recursive: true });
  writeFileSync(join(dir, 'design/gameplay.png'), 'concept');
  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  writePngHeader(join(dir, 'assets/ui/panel.png'), 100, 50);
  writeJson(join(dir, 'assets/asset-manifest.json'), {
    assets: [{
      key: 'ui-panel',
      category: 'ui',
      path: 'assets/ui/panel.png',
      width: 100,
      height: 50,
      maxBox: [200, 100],
    }],
  });
  writeJson(join(dir, '.playtest/inputs/layout-contract.json'), {
    canvas: { designWidth: 400, designHeight: 200 },
    scenes: {
      GameScene: {
        elements: [{
          key: 'ui-panel',
          role: 'panel',
          kind: 'static',
          box: [0.4, 0.4, 0.2, 0.2],
        }],
      },
    },
  });
  const renderedPath = join(
    dir,
    '.playtest/verify_middle/layout/GameScene.rendered.json',
  );
  const writeRendered = (w, h) => writeJson(renderedPath, {
    scene: 'GameScene',
    run_id: runId,
    canvas: { w: 400, h: 200 },
    elements: [{
      name: 'ui-panel',
      layoutKey: 'ui-panel',
      type: 'Image',
      cx: 200,
      cy: 100,
      w,
      h,
      visible: true,
      effScrollFactorX: 0,
      effScrollFactorY: 0,
    }],
  });
  writeRendered(150, 75);
  writeJson(join(dir, '.playtest/verify_middle/layout/_run.json'), {
    source: 'capture-layout.mjs',
    run_id: runId,
    scenes: ['GameScene'],
  });

  let result = runGate(dir, 'layout_alignment');
  assert.equal(result.status, 0, result.stderr);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'art_directed' });
  result = runGate(dir, 'layout_alignment');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /art_directed requires native 1:1 rendering/);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'pixel_art' });
  result = runGate(dir, 'layout_alignment');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /pixel_art requires one positive integer uniform scale/);

  writeJson(join(dir, '.playtest/state.json'), { quality_profile: 'responsive' });
  writeRendered(150, 90);
  result = runGate(dir, 'layout_alignment');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /non-uniform render scale/);
});

// ── P3: structured BLOCKED output ────────────────────────────────────────

test('P3 failing gate emits a structured JSON payload on stdout + BLOCKED shim on stderr', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  const result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0, result.stderr);

  // Machine-readable payload on stdout.
  const payload = JSON.parse(result.stdout);
  assert.equal(payload.gate, 'asset_manifest');
  assert.equal(payload.blocked, true);
  assert.ok(Array.isArray(payload.failures) && payload.failures.length > 0);
  for (const f of payload.failures) {
    assert.equal(typeof f.message, 'string');
    assert.ok(f.message.length > 0);
    assert.ok(f.check, 'each failure records its originating check');
    // The compat stderr line has no `BLOCKED:` prefix in the structured message.
    assert.doesNotMatch(f.message, /^BLOCKED:/);
  }

  // Backward-compatible human shim still on stderr.
  assert.match(result.stderr, /BLOCKED:/);
  assert.match(result.stderr, /asset-manifest\.json missing/);
});

test('P3 check metadata provides axis + agent routing for composite checks', () => {
  const ui = metadataFor('ui_contract');
  assert.equal(ui.axis, 'ui');
  assert.equal(ui.agent, 'asset-integration-agent');
  assert.equal(
    metadataFor('asset_runtime_references').agent,
    'asset-integration-agent',
  );
  assert.equal(metadataFor('release_waivers').agent, 'orchestrator');
  assert.equal(metadataFor('critic').agent, 'critic-agent');
  assert.equal(metadataFor('performance').axis, 'performance');
  assert.equal(metadataFor('entity_scale').agent, 'asset-integration-agent');
  // Every check in the iteration list has routing metadata so the main agent
  // can group + dispatch blockers deterministically.
  const contract = JSON.parse(runGate(fixtureForContract(), 'gate_contract').stdout);
  for (const name of contract.iteration_ready) {
    assert.ok(CHECK_METADATA[name], `missing metadata for check: ${name}`);
  }
});

function fixtureForContract() {
  const dir = mkdtempSync(join(tmpdir(), 'gca-gate-meta-'));
  writeJson(join(dir, 'gca.config.json'), { gates: {} });
  return dir;
}

// Regression: a direct-dispatch gate (the shape the P4 diagnostic tools run —
// gateXxx() called outside runChecks, so blockingIssues is empty) must still
// enrich each structured failure with axis/agent via the invoked gate's own
// metadata, not just a bare {check, message}.
test('P3 direct-dispatch gate failures carry axis + agent (diagnostic-tool path)', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  const result = runGate(dir, 'asset_manifest');
  assert.notEqual(result.status, 0, result.stderr);
  const { failures } = JSON.parse(result.stdout);
  assert.ok(failures.length > 0);
  for (const f of failures) {
    assert.equal(f.check, 'asset_manifest');
    assert.equal(f.axis, 'assets');
    assert.equal(f.agent, 'scaffold-agent');
    assert.equal(typeof f.message, 'string');
  }
});

// Regression: the three P4 diagnostic tools dispatch these gate names directly,
// so each must resolve routing metadata (td_contract was previously missing).
test('P3 diagnostic-tool gate names all resolve axis + agent metadata', () => {
  for (const gate of ['ui_contract', 'td_contract', 'interaction_contract']) {
    const meta = metadataFor(gate);
    assert.ok(meta.axis, `${gate} missing axis`);
    assert.ok(meta.agent, `${gate} missing agent`);
  }
});

// Regression: structured failures[] must correspond 1:1 with the stderr BLOCKED
// lines for a composite gate (nothing dropped, nothing duplicated).
test('P3 structured failures mirror every stderr BLOCKED line 1:1', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  writeRunIdentity(dir);
  const result = runGate(dir, 'iteration_ready');
  assert.notEqual(result.status, 0, result.stderr);
  const { failures } = JSON.parse(result.stdout);
  const stderrBlocked = result.stderr
    .split('\n')
    .filter((line) => /^\s*BLOCKED:/.test(line));
  assert.ok(failures.length > 1, 'composite should produce multiple failures');
  assert.equal(failures.length, stderrBlocked.length);
  assert.ok(failures.every((f) => f.check && f.message));
});

// Regression: a composite gate runs gateStateJson(true) BEFORE runChecks. When
// that pre-check fails alongside runChecks failures, its failure must NOT be
// dropped from the structured payload (the previous form used blockingIssues
// wholesale, which only captured runChecks failures). Corrupt the state digest
// so the pre-check fails, then assert the payload still mirrors stderr 1:1.
test('P3 composite keeps failures produced outside runChecks (gateStateJson pre-check)', (t) => {
  const dir = fixture(t);
  writeBasicGame(dir);
  writeRunIdentity(dir);
  const statePath = join(dir, '.playtest/state.json');
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.build_digest = 'sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef';
  writeJson(statePath, state);
  const result = runGate(dir, 'iteration_ready');
  assert.notEqual(result.status, 0, result.stderr);
  const { failures } = JSON.parse(result.stdout);
  const stderrBlocked = result.stderr
    .split('\n')
    .filter((line) => /^\s*BLOCKED:/.test(line));
  assert.equal(failures.length, stderrBlocked.length);
  assert.ok(failures.every((f) => f.check && f.message));
});
