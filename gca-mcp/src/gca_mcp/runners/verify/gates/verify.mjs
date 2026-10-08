/** Verification evidence gates — mechanically extracted gate domain. */

import {
  readFileSync,
  readdirSync,
  statSync,
  lstatSync,
  realpathSync,
} from 'fs';
import { createHash } from 'crypto';
import {
  join,
  relative,
  resolve,
  sep,
} from 'path';

import {
  resolveRead,
  RECORDING_DIR,
  VERIFY_REPORT,
  VERIFY_RUN,
  RUNTIME_TOKEN_RECEIPT,
  RUNTIME_ASSETS_RECEIPT,
  VIDEO_RECEIPT_NAME,
  TEST_RESULTS,
  PLAY_REPORT,
  PLAY_HISTORY,
  CRITIC_REPORT,
  VISUAL_DIFF_REPORT,
  VISUAL_DIFF_HISTORY,
} from '../playtest-layout.mjs';

import {
  readJSON,
  sha256File,
  stableJson,
  listFiles,
  isFiniteNumber,
} from '../shared/gate-io.mjs';
import {
  args,
  gameDir,
  playtestDir,
  fail,
  fileExists,
} from '../gate-context.mjs';
import {
  readGcaConfig,
  canonicalBuildDigest,
  PHASER_RUNTIME_TEXTURE_KEY_RE,
  inspectRuntimeAssetPath,
  collectLayoutSourceContract,
  allowedRuntimeTextureKeys,
  isAllowedRuntimeTextureKey,
  latestSrcMtime,
  latestInputMtime,
  manifestEntries,
} from './support.mjs';

function gateVisualVerified(state) {
  if (!state) return;
  if (!state.visual_verified) fail('visual_verified is false — screenshots not reviewed');
}

// `verify.skip_video: true` disables the whole recording pipeline
// (record_with_state + analyze_video + performance capture), so the gates that
// need its artifacts auto-N/A instead of blocking a build that can never
// produce them.
function videoVerifySkipped() {
  return readGcaConfig().verify?.skip_video === true;
}

function gatePlaythroughRecorded(state) {
  if (videoVerifySkipped()) return;
  const recordings = listFiles(
    join(playtestDir, RECORDING_DIR),
    (_path, name) => name === 'recording.webm',
  );
  if (recordings.length === 0) {
    fail('no recording.webm found for the release verify run');
    return;
  }
  const srcM = latestSrcMtime();
  const newestRecordingM = Math.max(...recordings.map((path) => statSync(path).mtimeMs));
  if (srcM && newestRecordingM < srcM) {
    fail('playthrough recording is stale (older than latest src/ change)');
  }
}

function gateReleaseWaivers(state) {
  if (!videoVerifySkipped()) return;
  const waivers = Array.isArray(state?.release_waivers)
    ? state.release_waivers
    : [];
  const waiver = waivers.find((item) =>
    item && item.id === 'video-performance'
  );
  if (
    !waiver
    || typeof waiver.reason !== 'string'
    || waiver.reason.trim().length < 12
    || typeof waiver.approved_by !== 'string'
    || !waiver.approved_by.trim()
    || typeof waiver.approval_event_id !== 'string'
    || !waiver.approval_event_id.startsWith('waiver-')
    || !validIsoTimestamp(waiver.approved_at)
    || Date.parse(waiver.approved_at) > Date.now() + 5 * 60 * 1000
    || waiver.pipeline_run_id !== state?.pipeline_run_id
    || waiver.build_digest !== state?.build_digest
  ) {
    fail(
      'verify.skip_video=true requires an explicit state.release_waivers[] '
      + 'entry {id:"video-performance", reason, approved_by, approved_at}; '
      + 'write_pipeline_state binds it to the current pipeline/build. '
      + 'Release evidence cannot disappear through a silent config default',
    );
  }
}

const VERIFY_IDENTITY_KEYS = ['pipeline_run_id', 'build_digest', 'verify_run_id'];

function resolveSealedArtifact(relativePath, expectedPath, label) {
  if (typeof relativePath !== 'string' || !relativePath) {
    fail(`${label} path is missing`);
    return null;
  }
  const candidate = resolve(gameDir, relativePath);
  const expected = resolve(gameDir, expectedPath);
  if (
    candidate !== expected
    || (candidate !== gameDir && !candidate.startsWith(gameDir + sep))
  ) {
    fail(`${label} path '${relativePath}' is not the canonical workspace path`);
    return null;
  }
  if (!fileExists(candidate)) {
    fail(`${label} is missing at ${relativePath}`);
    return null;
  }
  try {
    const expectedReal = resolve(
      realpathSync(gameDir),
      relative(gameDir, candidate),
    );
    if (
      lstatSync(candidate).isSymbolicLink()
      || !lstatSync(candidate).isFile()
      || realpathSync(candidate) !== expectedReal
    ) {
      fail(`${label} must be a canonical regular file without symlink traversal`);
      return null;
    }
  } catch {
    fail(`${label} cannot be resolved safely`);
    return null;
  }
  return candidate;
}

function validateArtifactEntry(entry, expectedPath, label) {
  if (!entry || typeof entry !== 'object') {
    fail(`verify-report artifact entry for ${label} is missing`);
    return null;
  }
  const path = resolveSealedArtifact(entry.path, expectedPath, label);
  if (!path) return null;
  if (!/^[a-f0-9]{64}$/.test(String(entry.sha256 || ''))) {
    fail(`verify-report artifact entry for ${label} is missing sha256`);
  } else if (entry.sha256 !== sha256File(path)) {
    fail(`${label} changed after verify-report finalization`);
  }
  if (Number(entry.size_bytes) !== statSync(path).size) {
    fail(`${label} size does not match its finalized verify-report entry`);
  }
  return path;
}

function validateRuntimeIdentity(runtimeIdentity, currentRun, label) {
  if (
    !runtimeIdentity
    || typeof runtimeIdentity !== 'object'
    || runtimeIdentity.schema_version !== 1
    || runtimeIdentity.source !== 'runtime-identity.mjs'
    || runtimeIdentity.verified !== true
  ) {
    fail(`${label} has no producer-verified runtime_identity`);
    return false;
  }
  const expected = {
    epoch_url: currentRun.url,
    page_url: currentRun.url,
    runtime_token_url: currentRun.runtime_token_url,
    fetched_url: currentRun.runtime_token_url,
    runtime_token_sha256: currentRun.runtime_token_sha256,
    runtime_receipt_sha256: currentRun.runtime_receipt_sha256,
    pipeline_run_id: currentRun.pipeline_run_id,
    build_digest: currentRun.build_digest,
    verify_run_id: currentRun.verify_run_id,
  };
  const mismatches = Object.entries(expected)
    .filter(([key, value]) => runtimeIdentity[key] !== value)
    .map(([key]) => key);
  if (mismatches.length > 0) {
    fail(`${label} runtime_identity is stale/cross-run in: ${mismatches.join(', ')}`);
    return false;
  }
  return true;
}

function isSortedUniqueStrings(value) {
  return (
    Array.isArray(value)
    && value.every((item) => typeof item === 'string' && item.length > 0)
    && new Set(value).size === value.length
    && value.every((item, index) => index === 0 || value[index - 1] < item)
  );
}

const RUNTIME_TEXTURE_BINDING_FIELDS = [
  'bind',
  'instance',
  'layout_key',
  'name',
  'object_type',
  'scene',
  'texture_key',
  'visible',
].sort();

function runtimeTextureBindingValues(binding) {
  return [
    binding.scene,
    binding.texture_key,
    binding.object_type,
    binding.name || '',
    binding.layout_key || '',
    binding.bind || '',
    binding.instance,
    binding.visible ? 1 : 0,
  ];
}

function compareRuntimeTextureBindings(left, right) {
  const a = runtimeTextureBindingValues(left);
  const b = runtimeTextureBindingValues(right);
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] === b[index]) continue;
    return a[index] < b[index] ? -1 : 1;
  }
  return 0;
}

function canonicalRuntimeTextureBindings(value, expectedScene = null) {
  if (!Array.isArray(value)) return null;
  const normalized = [];
  for (const row of value) {
    const fields = row && typeof row === 'object' && !Array.isArray(row)
      ? Object.keys(row).sort()
      : [];
    if (
      stableJson(fields) !== stableJson(RUNTIME_TEXTURE_BINDING_FIELDS)
      || typeof row.scene !== 'string'
      || !row.scene
      || (expectedScene !== null && row.scene !== expectedScene)
      || typeof row.texture_key !== 'string'
      || !row.texture_key
      || typeof row.object_type !== 'string'
      || !row.object_type
      || !Number.isInteger(row.instance)
      || row.instance < 0
      || row.visible !== true
      || ['name', 'layout_key', 'bind'].some(
        (field) => (
          row[field] !== null
          && (typeof row[field] !== 'string' || !row[field])
        ),
      )
    ) return null;
    normalized.push({
      scene: row.scene,
      texture_key: row.texture_key,
      object_type: row.object_type,
      name: row.name,
      layout_key: row.layout_key,
      bind: row.bind,
      instance: row.instance,
      visible: true,
    });
  }
  const sorted = [...normalized].sort(compareRuntimeTextureBindings);
  const signatures = sorted.map((row) => stableJson(runtimeTextureBindingValues(row)));
  if (
    new Set(signatures).size !== signatures.length
    || stableJson(normalized) !== stableJson(sorted)
  ) return null;
  return sorted;
}

function runtimeTextureBindingMatchesNode(binding, runtimeNode) {
  if (binding.scene !== runtimeNode.scene) return false;
  const nodeIdentities = new Set(
    [runtimeNode.node?.key, runtimeNode.node?.bind]
      .filter((value) => typeof value === 'string' && value),
  );
  const bindingIdentities = new Set(
    [binding.name, binding.layout_key, binding.bind]
      .filter((value) => typeof value === 'string' && value),
  );
  if (![...bindingIdentities].some((identity) => nodeIdentities.has(identity))) {
    return false;
  }
  const type = String(binding.object_type || '').toLowerCase().replace(/[^a-z]/g, '');
  if (runtimeNode.source === 'live-text') return type === 'text';
  const textureObjectTypes = new Set([
    'image',
    'sprite',
    'tilesprite',
    'nineslice',
    'rendertexture',
    'mesh',
    'rope',
    'shader',
    'video',
  ]);
  return (
    (runtimeNode.source === 'procedural' || runtimeNode.source === 'composition')
    && textureObjectTypes.has(type)
  );
}

function runtimeTextureBindingMatchesAnchoredText(binding, fileNode, manifestByKey) {
  if (binding.scene !== fileNode.scene) return false;
  const type = String(binding.object_type || '').toLowerCase().replace(/[^a-z]/g, '');
  if (type !== 'text') return false;
  const hostKey = fileNode.node?.key;
  if (
    typeof hostKey !== 'string'
    || !hostKey
    || binding.layout_key !== hostKey
  ) return false;
  const manifestEntry = manifestByKey.get(hostKey);
  if (
    !manifestEntry
    || manifestEntry.sourceStrategy !== fileNode.source
    || !Array.isArray(manifestEntry.anchors)
  ) return false;
  return manifestEntry.anchors.some((anchor) => {
    if (!anchor || typeof anchor !== 'object' || Array.isArray(anchor)) return false;
    const anchorId = anchor.id;
    const anchorBind = anchor.binds ?? anchor.bind;
    return (
      typeof anchorId === 'string'
      && anchorId
      && typeof anchorBind === 'string'
      && anchorBind
      && binding.name === anchorId
      && binding.bind === anchorBind
    );
  });
}

function spriteActionRequiresAnimation(owner, action, spec) {
  const metadataPath = spec?.metadata;
  const inspected = inspectRuntimeAssetPath(metadataPath);
  if (!inspected.ok || !inspected.exists) {
    fail(
      `runtime sprite '${owner}' action '${action}' cannot verify animation `
      + `contract from metadata '${metadataPath || '<missing>'}'`,
    );
    return null;
  }
  const metadata = readJSON(inspected.path);
  const frameCount = metadata?.frameCount;
  if (!Number.isInteger(frameCount) || frameCount < 1) {
    fail(
      `runtime sprite '${owner}' action '${action}' metadata must declare `
      + 'positive integer frameCount',
    );
    return null;
  }
  return frameCount > 1;
}

function validateRuntimeAssetRegistry(currentRun, report) {
  const rel = `.playtest/${RUNTIME_ASSETS_RECEIPT}`;
  const entry = report.artifacts?.runtime_asset_registry;
  const path = validateArtifactEntry(entry, rel, 'runtime asset registry');
  if (!path) return;
  const registry = readJSON(path);
  if (
    !registry
    || registry.schema_version !== 1
    || registry.producer !== 'gca-mcp.capture_runtime_assets'
    || VERIFY_IDENTITY_KEYS.some((key) => registry[key] !== currentRun[key])
    || registry.url !== currentRun.url
    || registry.runtime_token_sha256 !== currentRun.runtime_token_sha256
    || registry.runtime_receipt_sha256 !== currentRun.runtime_receipt_sha256
  ) {
    fail('runtime-assets.json has invalid producer/epoch identity');
    return;
  }
  validateRuntimeIdentity(registry.runtime_identity, currentRun, 'runtime-assets.json');
  if (stableJson(registry) !== stableJson(report.runtime_asset_registry)) {
    fail('runtime-assets.json no longer matches the registry sealed in verify-report.json');
  }

  const manifestPath = join(gameDir, 'assets', 'asset-manifest.json');
  if (
    registry.manifest !== 'assets/asset-manifest.json'
    || !fileExists(manifestPath)
    || registry.manifest_sha256 !== sha256File(manifestPath)
  ) {
    fail('runtime-assets.json is stale relative to asset-manifest.json');
    return;
  }
  const arrayFields = [
    'loaded_texture_keys',
    'animation_frame_texture_keys',
    'display_list_texture_keys',
    'active_scene_keys',
    'registered_scene_keys',
    'reachable_scene_keys',
  ];
  for (const field of arrayFields) {
    if (!isSortedUniqueStrings(registry[field])) {
      fail(`runtime-assets.json ${field} must be a sorted unique string array`);
    }
  }
  const loaded = new Set(registry.loaded_texture_keys || []);
  const animated = new Set(registry.animation_frame_texture_keys || []);
  const displayed = new Set(registry.display_list_texture_keys || []);
  const manifest = readJSON(manifestPath);
  if (
    !registry.game_config
    || typeof registry.game_config !== 'object'
    || Array.isArray(registry.game_config)
    || stableJson(Object.keys(registry.game_config).sort()) !== stableJson(['pixelArt'])
    || typeof registry.game_config.pixelArt !== 'boolean'
  ) {
    fail('runtime-assets.json game_config must report boolean game.config.pixelArt');
  } else if (
    currentRun.quality_profile === 'pixel_art'
    && registry.game_config.pixelArt !== true
  ) {
    fail(
      '[pixel-art-runtime] quality_profile pixel_art requires captured runtime '
      + 'game.config.pixelArt === true for nearest-neighbor filtering',
    );
  }
  for (const key of (registry.active_scene_keys || [])) {
    if (!(registry.registered_scene_keys || []).includes(key)) {
      fail(`runtime-assets.json active scene '${key}' is not registered`);
    }
  }

  const scenes = Array.isArray(registry.scenes) ? registry.scenes : [];
  const sceneKeys = scenes.map((scene) => scene?.scene);
  const flattenedBindings = [];
  if (
    scenes.length === 0
    || sceneKeys.some((key) => typeof key !== 'string' || !key)
    || new Set(sceneKeys).size !== sceneKeys.length
  ) {
    fail('runtime-assets.json scenes must enumerate each registered scene exactly once');
  } else {
    const registered = [...(registry.registered_scene_keys || [])];
    if (stableJson([...sceneKeys].sort()) !== stableJson(registered)) {
      fail('runtime-assets.json scene registry does not match registered_scene_keys');
    }
    const reachable = scenes
      .filter((scene) => scene?.reachable === true)
      .map((scene) => scene.scene)
      .sort();
    if (stableJson(reachable) !== stableJson(registry.reachable_scene_keys || [])) {
      fail('runtime-assets.json reachable scenes do not match per-scene evidence');
    }
    const unreachable = registered.filter(
      (sceneKey) => !(registry.reachable_scene_keys || []).includes(sceneKey),
    );
    if (unreachable.length > 0) {
      fail(
        `runtime-assets.json has ${unreachable.length} registered scene(s) that `
        + `cannot be started/reached: ${unreachable.slice(0, 8).join(', ')}`,
      );
    }
    for (const scene of scenes) {
      if (!isSortedUniqueStrings(scene?.texture_keys)) {
        fail(`runtime-assets.json scene '${scene?.scene || '<missing>'}' has invalid texture_keys`);
        continue;
      }
      const bindings = canonicalRuntimeTextureBindings(
        scene.texture_bindings,
        scene.scene,
      );
      if (!bindings) {
        fail(
          `runtime-assets.json scene '${scene.scene}' has invalid, duplicate, `
          + 'or non-canonical texture_bindings',
        );
        continue;
      }
      flattenedBindings.push(...bindings);
      const derivedKeys = [...new Set(
        bindings.map((binding) => binding.texture_key),
      )].sort();
      if (stableJson(derivedKeys) !== stableJson(scene.texture_keys)) {
        fail(
          `runtime-assets.json scene '${scene.scene}' texture_keys do not match `
          + 'its display-object texture_bindings',
        );
      }
      for (const key of scene.texture_keys) {
        if (!displayed.has(key)) {
          fail(`runtime-assets.json scene '${scene.scene}' texture '${key}' is absent from display registry`);
        }
      }
    }
  }
  flattenedBindings.sort(compareRuntimeTextureBindings);
  const bindingSignatures = flattenedBindings.map(
    (binding) => stableJson(runtimeTextureBindingValues(binding)),
  );
  if (new Set(bindingSignatures).size !== bindingSignatures.length) {
    fail('runtime-assets.json repeats a display-object texture binding across scenes');
  }
  const sealedBindings = canonicalRuntimeTextureBindings(
    registry.display_object_texture_bindings,
  );
  if (!sealedBindings) {
    fail(
      'runtime-assets.json display_object_texture_bindings must be sorted, '
      + 'unique, and structurally valid',
    );
  } else if (stableJson(sealedBindings) !== stableJson(flattenedBindings)) {
    fail(
      'runtime-assets.json top-level display-object bindings do not match '
      + 'per-scene evidence',
    );
  }
  const bindingDisplayKeys = [...new Set(
    flattenedBindings.map((binding) => binding.texture_key),
  )].sort();
  if (stableJson(bindingDisplayKeys) !== stableJson([...displayed].sort())) {
    fail(
      'runtime-assets.json display_list_texture_keys do not match recomputed '
      + 'display-object bindings',
    );
  }

  const layoutSources = collectLayoutSourceContract();
  const runtimeNodes = layoutSources.runtimeNodes;
  const fileNodes = layoutSources.fileNodes;
  const manifestByKey = new Map(
    manifestEntries(manifest)
      .filter((entry) => entry && typeof entry.key === 'string' && entry.key)
      .map((entry) => [entry.key, entry]),
  );
  const validRuntimeUuidKeys = new Set();
  const uuidKeys = [...new Set([...loaded, ...animated, ...displayed])]
    .filter((key) => PHASER_RUNTIME_TEXTURE_KEY_RE.test(key))
    .sort();
  for (const key of uuidKeys) {
    if (!displayed.has(key)) {
      fail(
        `runtime UUID texture '${key}' is loaded-only; UUID textures must be `
        + 'actually displayed and bound to a declared runtime layout node',
      );
      continue;
    }
    const bindings = flattenedBindings.filter(
      (binding) => binding.texture_key === key,
    );
    if (!bindings.some(
      (binding) => (
        runtimeNodes.some(
          (runtimeNode) => runtimeTextureBindingMatchesNode(binding, runtimeNode),
        )
        || fileNodes.some(
          (fileNode) => runtimeTextureBindingMatchesAnchoredText(
            binding,
            fileNode,
            manifestByKey,
          ),
        )
      ),
    )) {
      fail(
        `runtime UUID texture '${key}' is not bound to a recursively declared `
        + 'runtime layout node or manifest-declared file-backed text anchor '
        + 'with compatible scene/host/id/bind semantics',
      );
      continue;
    }
    validRuntimeUuidKeys.add(key);
  }

  const allowedTextureKeys = allowedRuntimeTextureKeys(manifest);
  for (const [field, keys] of [
    ['loaded_texture_keys', loaded],
    ['animation_frame_texture_keys', animated],
    ['display_list_texture_keys', displayed],
  ]) {
    const undeclared = [...keys]
      .filter((key) => (
        !isAllowedRuntimeTextureKey(key, allowedTextureKeys)
        && !validRuntimeUuidKeys.has(key)
      ))
      .sort();
    if (undeclared.length > 0) {
      fail(
        `runtime-assets.json ${field} contains undeclared texture key(s): `
        + undeclared.slice(0, 8).join(', '),
      );
    }
  }
  for (const key of animated) {
    if (!loaded.has(key)) fail(`runtime-assets.json animation texture '${key}' was not loaded`);
  }
  for (const key of displayed) {
    if (!loaded.has(key)) fail(`runtime-assets.json displayed texture '${key}' was not loaded`);
  }

  const entries = manifest ? manifestEntries(manifest) : null;
  const assets = Array.isArray(registry.manifest_assets) ? registry.manifest_assets : null;
  if (!entries || !assets || assets.length !== entries.length) {
    fail('runtime-assets.json manifest_assets does not cover the canonical manifest');
    return;
  }
  const byKey = new Map(assets.map((item) => [item?.key, item]));
  if (byKey.size !== assets.length) {
    fail('runtime-assets.json manifest_assets contains duplicate or missing keys');
  }
  for (const manifestEntry of entries) {
    const key = manifestEntry?.key;
    const actual = byKey.get(key);
    const declared = new Set([key]);
    if (typeof manifestEntry?.textureKey === 'string' && manifestEntry.textureKey) {
      declared.add(manifestEntry.textureKey);
    }
    for (const spec of Object.values(manifestEntry?.actions || {})) {
      if (typeof spec?.textureKey === 'string' && spec.textureKey) declared.add(spec.textureKey);
    }
    const expectedDeclared = [...declared].sort();
    const expectedLoaded = expectedDeclared.filter((item) => loaded.has(item));
    const expectedAnimated = expectedDeclared.filter((item) => animated.has(item));
    const expectedDisplayed = expectedDeclared.filter((item) => displayed.has(item));
    const actions = (
      manifestEntry?.actions
      && typeof manifestEntry.actions === 'object'
      && !Array.isArray(manifestEntry.actions)
    )
      ? Object.entries(manifestEntry.actions)
      : [];
    let expectedLoadedState = expectedLoaded.length > 0;
    let expectedUsedState = expectedAnimated.length > 0 || expectedDisplayed.length > 0;
    if (actions.length > 0) {
      const actionContracts = [];
      for (const [action, spec] of actions) {
        const textureKey = spec?.textureKey;
        const loadedAction = typeof textureKey === 'string' && loaded.has(textureKey);
        if (!loadedAction) {
          fail(
            `runtime sprite '${key}' action '${action}' texture `
            + `'${textureKey || '<missing>'}' was not loaded`,
          );
        }
        const animationRequired = spriteActionRequiresAnimation(key, action, spec);
        if (
          animationRequired === true
          && typeof textureKey === 'string'
          && !animated.has(textureKey)
        ) {
          fail(
            `runtime sprite '${key}' animated action '${action}' texture `
            + `'${textureKey}' does not participate in any animation frames`,
          );
        }
        actionContracts.push({
          loaded: loadedAction,
          animationSatisfied: animationRequired === false
            || (
              animationRequired === true
              && typeof textureKey === 'string'
              && animated.has(textureKey)
            ),
        });
      }
      expectedLoadedState = actionContracts.every((contract) => contract.loaded);
      expectedUsedState = (
        expectedLoadedState
        && actionContracts.every((contract) => contract.animationSatisfied)
      );
    }
    if (
      !actual
      || stableJson(actual.declared_texture_keys) !== stableJson(expectedDeclared)
      || stableJson(actual.loaded_texture_keys) !== stableJson(expectedLoaded)
      || stableJson(actual.animation_frame_texture_keys) !== stableJson(expectedAnimated)
      || stableJson(actual.display_list_texture_keys) !== stableJson(expectedDisplayed)
      || actual.loaded !== expectedLoadedState
      || actual.used !== expectedUsedState
    ) {
      fail(`runtime-assets.json manifest asset '${key || '<missing>'}' has a forged/stale registry row`);
      continue;
    }
    if (actual.loaded !== true || actual.used !== true) {
      fail(`runtime manifest asset '${key}' was not both loaded and used`);
    }
  }
  const expectedSummary = {
    loaded_texture_count: loaded.size,
    animation_texture_count: animated.size,
    display_texture_count: displayed.size,
    display_object_texture_binding_count: flattenedBindings.length,
    runtime_uuid_texture_count: [...displayed]
      .filter((key) => PHASER_RUNTIME_TEXTURE_KEY_RE.test(key)).length,
    registered_scene_count: new Set(registry.registered_scene_keys || []).size,
    reachable_scene_count: new Set(registry.reachable_scene_keys || []).size,
    manifest_asset_count: assets.length,
    manifest_loaded_count: assets.filter((item) => item?.loaded === true).length,
    manifest_used_count: assets.filter((item) => item?.used === true).length,
  };
  for (const [key, value] of Object.entries(expectedSummary)) {
    if (registry.summary?.[key] !== value) {
      fail(`runtime-assets.json summary.${key} does not match the sealed registry`);
    }
  }
}

function artifactEntryMap(entries) {
  return new Map(
    (Array.isArray(entries) ? entries : [])
      .filter((entry) => entry && typeof entry.path === 'string')
      .map((entry) => [entry.path, entry]),
  );
}

function validateVideoReceiptChain(currentRun, report) {
  if (videoVerifySkipped()) return;
  const receiptEntries = artifactEntryMap(report.artifacts?.video_receipts);
  const recordingEntries = artifactEntryMap(report.artifacts?.recordings);
  const timelineEntries = artifactEntryMap(report.artifacts?.state_timelines);
  const performanceEntries = artifactEntryMap(report.artifacts?.performance_summaries);
  const analysisEntries = artifactEntryMap(report.artifacts?.video_analyses);
  const embeddedReceipts = Array.isArray(report.video_receipts)
    ? report.video_receipts
    : [];
  const root = join(playtestDir, RECORDING_DIR);
  const scenarios = fileExists(root)
    ? readdirSync(root)
      .filter((name) => /^[A-Za-z0-9_-]+$/.test(name))
      .filter((name) => {
        const path = join(root, name);
        try {
          return lstatSync(path).isDirectory() && !lstatSync(path).isSymbolicLink();
        } catch {
          return false;
        }
      })
      .filter((name) => fileExists(join(root, name, 'recording.webm')))
      .sort()
    : [];
  if (scenarios.length === 0) {
    fail('no canonical scenario video receipt chain exists for this finalized verify run');
    return;
  }
  if (embeddedReceipts.length !== scenarios.length) {
    fail('verify-report.json does not seal every scenario video receipt');
  }

  for (const scenario of scenarios) {
    const baseRel = `.playtest/${RECORDING_DIR}/${scenario}`;
    const receiptRel = `${baseRel}/${VIDEO_RECEIPT_NAME}`;
    const videoRel = `${baseRel}/recording.webm`;
    const timelineRel = `${baseRel}/state-timeline.json`;
    const performanceRel = `${baseRel}/performance-summary.json`;
    const analysisRel = `${baseRel}/video-analysis.json`;
    const receiptPath = validateArtifactEntry(
      receiptEntries.get(receiptRel),
      receiptRel,
      `[${scenario}] video receipt`,
    );
    const videoPath = validateArtifactEntry(
      recordingEntries.get(videoRel),
      videoRel,
      `[${scenario}] recording`,
    );
    const timelinePath = validateArtifactEntry(
      timelineEntries.get(timelineRel),
      timelineRel,
      `[${scenario}] state timeline`,
    );
    const performancePath = validateArtifactEntry(
      performanceEntries.get(performanceRel),
      performanceRel,
      `[${scenario}] performance summary`,
    );
    const analysisPath = validateArtifactEntry(
      analysisEntries.get(analysisRel),
      analysisRel,
      `[${scenario}] video analysis`,
    );
    if (!receiptPath || !videoPath || !timelinePath || !performancePath || !analysisPath) {
      continue;
    }
    const receipt = readJSON(receiptPath);
    if (
      !receipt
      || receipt.schema_version !== 1
      || receipt.producer !== 'gca-mcp.record_with_state'
      || VERIFY_IDENTITY_KEYS.some((key) => receipt[key] !== currentRun[key])
      || receipt.url !== currentRun.url
      || receipt.runtime_token_sha256 !== currentRun.runtime_token_sha256
      || receipt.runtime_receipt_sha256 !== currentRun.runtime_receipt_sha256
      || receipt.video_path !== videoRel
      || receipt.timeline_path !== timelineRel
      || receipt.performance_path !== performanceRel
      || receipt.video_sha256 !== sha256File(videoPath)
      || receipt.timeline_sha256 !== sha256File(timelinePath)
      || receipt.performance_sha256 !== sha256File(performancePath)
    ) {
      fail(`[${scenario}] video receipt is stale, altered, or cross-run`);
      continue;
    }
    validateRuntimeIdentity(receipt.runtime_identity, currentRun, `[${scenario}] video receipt`);
    if (!embeddedReceipts.some((item) => stableJson(item) === stableJson(receipt))) {
      fail(`[${scenario}] video receipt differs from the copy sealed in verify-report.json`);
    }
    const analysis = readJSON(analysisPath);
    if (
      !analysis
      || analysis.producer !== 'gca-mcp.analyze_video'
      || VERIFY_IDENTITY_KEYS.some((key) => analysis[key] !== currentRun[key])
      || analysis.url !== currentRun.url
      || analysis.runtime_token_sha256 !== currentRun.runtime_token_sha256
      || analysis.runtime_receipt_sha256 !== currentRun.runtime_receipt_sha256
      || analysis.video_receipt !== receiptRel
      || analysis.video_receipt_sha256 !== sha256File(receiptPath)
      || analysis.video_path !== videoRel
      || analysis.video_sha256 !== receipt.video_sha256
      || analysis.timeline_path !== timelineRel
      || analysis.timeline_sha256 !== receipt.timeline_sha256
      || analysis.analysis_status !== 'completed'
      || !Array.isArray(analysis.analysis_errors)
      || analysis.analysis_errors.length > 0
      || typeof analysis.analysis !== 'string'
      || !analysis.analysis.trim()
      || analysis.analysis.trimStart().toLowerCase().startsWith('analysis failed')
      || !Array.isArray(analysis.segment_analyses)
      || analysis.segment_analyses.length === 0
      || analysis.segment_analyses.some(
        (item) => (
          !item
          || typeof item !== 'object'
          || item.error
          || typeof item.analysis !== 'string'
          || !item.analysis.trim()
        ),
      )
    ) {
      fail(`[${scenario}] video analysis is not bound to its finalized video receipt`);
    } else {
      validateRuntimeIdentity(analysis.runtime_identity, currentRun, `[${scenario}] video analysis`);
    }
  }
}

function gateVerifyReport(allowedModes = null) {
  const reportPath = resolveRead(playtestDir, VERIFY_REPORT);
  if (!fileExists(reportPath)) {
    fail('verify-report.json missing — run write_verify_report after verification');
    return;
  }
  const report = readJSON(reportPath);
  if (!report) {
    fail('verify-report.json is invalid JSON');
    return;
  }
  if (report.schema_version !== 4) {
    fail(`verify-report.json schema_version must be 4 (got ${report.schema_version ?? 'missing'})`);
  }

  const state = readJSON(join(playtestDir, 'state.json'));
  const currentRun = readJSON(join(playtestDir, VERIFY_RUN));
  if (!currentRun || currentRun.schema_version !== 2) {
    fail('verify/current-run.json must be a schema_version 2 epoch receipt');
    return;
  }
  if (
    currentRun?.pipeline_run_id !== state?.pipeline_run_id
    || currentRun?.build_digest !== state?.build_digest
  ) {
    fail('verify/current-run.json is cross-pipeline or stale relative to state.json');
  }
  const identityKeys = ['pipeline_run_id', 'build_digest', 'verify_run_id'];
  for (const key of identityKeys) {
    const expected = key === 'verify_run_id' ? currentRun?.[key] : state?.[key];
    if (typeof expected !== 'string' || !expected || report[key] !== expected) {
      fail(
        `verify-report.json ${key} is missing/cross-run `
        + `(report=${report[key] || 'missing'}, expected=${expected || 'missing'})`,
      );
    }
  }
  for (const key of ['genre', 'interaction_profile', 'quality_profile', 'level_model']) {
    if (
      !state?.[key]
      || currentRun?.[key] !== state[key]
      || report[key] !== state[key]
    ) {
      fail(`verify-report.json ${key} is missing/cross-run relative to state/current-run`);
    }
  }
  if (report.run_id !== report.verify_run_id) {
    fail('verify-report.json run_id must equal verify_run_id');
  }
  const expectedReportRel = `.playtest/${VERIFY_REPORT}`;
  if (
    currentRun.status !== 'finalized'
    || currentRun.result_status !== report.status
    || currentRun.report !== expectedReportRel
    || currentRun.report_sha256 !== sha256File(reportPath)
  ) {
    fail('verify/current-run.json does not finalize and hash this exact verify-report.json');
  }
  if (
    typeof currentRun.url !== 'string'
    || currentRun.url !== report.url
    || currentRun.runtime_token_sha256 !== report.runtime_token_sha256
    || currentRun.runtime_receipt_sha256 !== report.runtime_receipt_sha256
    || typeof currentRun.runtime_token_url !== 'string'
    || !currentRun.runtime_token_url
  ) {
    fail('verify report URL/runtime-token identity does not match current-run.json');
  }
  const tokenRel = `.playtest/${RUNTIME_TOKEN_RECEIPT}`;
  const tokenPath = validateArtifactEntry(
    report.artifacts?.runtime_token_receipt,
    tokenRel,
    'runtime token receipt',
  );
  if (tokenPath) {
    const token = readJSON(tokenPath);
    const tokenSha = typeof token?.runtime_token === 'string'
      ? createHash('sha256').update(token.runtime_token).digest('hex')
      : null;
    if (
      token?.schema_version !== 1
      || token?.producer !== 'gca-mcp.begin_verify_run'
      || VERIFY_IDENTITY_KEYS.some((key) => token?.[key] !== currentRun[key])
      || token?.url !== currentRun.url
      || token?.runtime_token_url !== currentRun.runtime_token_url
      || token?.runtime_token_sha256 !== currentRun.runtime_token_sha256
      || tokenSha !== currentRun.runtime_token_sha256
      || sha256File(tokenPath) !== currentRun.runtime_receipt_sha256
    ) {
      fail('runtime-token.json is stale, altered, or cross-run');
    }
  }
  const actualDigest = canonicalBuildDigest();
  if (state?.build_digest !== actualDigest || report.build_digest !== actualDigest) {
    fail(
      `verification build_digest is stale `
      + `(actual=${actualDigest}, state=${state?.build_digest || 'missing'}, report=${report.build_digest || 'missing'})`,
    );
  }
  if (currentRun?.mode !== report.mode) {
    fail('verify/current-run.json does not match verify-report mode');
  }
  if (Array.isArray(allowedModes) && !allowedModes.includes(report.mode)) {
    fail(
      `verify-report mode '${report.mode || 'missing'}' is invalid for this gate `
      + `(expected ${allowedModes.join(' or ')})`,
    );
  }
  if (report.mode === 'full-novideo' && !videoVerifySkipped()) {
    fail("verify-report mode 'full-novideo' requires verify.skip_video=true");
  }

  const missing = report.summary?.missing_artifacts || [];
  if (missing.length > 0) {
    fail(`verify-report.json lists missing artifacts: ${missing.join(', ')}`);
  }
  if (report.status === 'blocked') {
    fail(`verify-report.json status is blocked (${Array.isArray(report.issues) ? report.issues.length : 0} issue(s))`);
  }
  if (report.status === 'partial') {
    const checkpoints = report.summary?.producer_checkpoints || {};
    const blockingPartial = (
      report.emergency_seal === true
      || Number(checkpoints.failed || 0) > 0
      || Number(checkpoints.interrupted || 0) > 0
      || (report.issues || []).some(
        (issue) => ['high', 'critical'].includes(String(issue?.severity || '').toLowerCase()),
      )
    );
    if (blockingPartial) {
      fail(`verify-report.json status is partial with incomplete/blocking evidence (${Array.isArray(report.issues) ? report.issues.length : 0} issue(s))`);
    }
  }

  validateRuntimeAssetRegistry(currentRun, report);
  if (report.mode === 'full') validateVideoReceiptChain(currentRun, report);

  const srcM = latestSrcMtime();
  const reportM = statSync(reportPath).mtimeMs;
  if (srcM && reportM < srcM) {
    fail('verify-report.json is stale (older than latest src/ change) — re-run verification and write_verify_report');
  }
}

function gateCriticReport() {
  const criticPath = resolveRead(playtestDir, CRITIC_REPORT);
  if (!fileExists(criticPath)) {
    fail('critic-report.json missing — dispatch the independent critic after the latest full verify run');
    return;
  }
  const critic = readJSON(criticPath);
  if (!critic || typeof critic !== 'object') {
    fail('critic-report.json is invalid JSON');
    return;
  }
  if (critic.verdict !== 'PASS') {
    fail(`critic verdict is ${critic.verdict || 'missing'} — independent review must PASS before ship`);
  }

  const requiredScores = [
    'visual_quality',
    'gameplay_mechanics',
    'completeness',
    'polish',
    'asset_fitness',
  ];
  for (const key of requiredScores) {
    const score = critic.scores?.[key];
    if (!Number.isFinite(score) || score < 0.7 || score > 1) {
      fail(`critic PASS is inconsistent: scores.${key}=${score} (each ship score must be 0.7..1)`);
    }
  }
  if (critic.verdict === 'PASS' && (critic.blocking_issues || []).length > 0) {
    fail('critic PASS is inconsistent with non-empty blocking_issues');
  }

  const verifyPath = resolveRead(playtestDir, VERIFY_REPORT);
  const verify = readJSON(verifyPath);
  if (Number(verify?.schema_version) >= 2) {
    if (
      Number(critic.schema_version) < 2
      || critic.source !== 'critic-agent'
      || !critic.verify_run_id
      || critic.verify_run_id !== verify.run_id
      || critic.pipeline_run_id !== verify.pipeline_run_id
      || critic.build_digest !== verify.build_digest
    ) {
      fail(
        `critic report is not bound to the latest pipeline/build/verify run `
        + `(critic=${critic.verify_run_id || 'missing'}, verify=${verify?.run_id || 'missing'})`,
      );
    }
  }

  const latestCriticInput = latestInputMtime([
    join(gameDir, 'GAME_DESIGN.md'),
    join(gameDir, 'design'),
    join(gameDir, 'assets'),
    verifyPath,
    resolveRead(playtestDir, PLAY_REPORT),
    resolveRead(playtestDir, VISUAL_DIFF_REPORT),
    join(playtestDir, 'verify', 'test-run', 'screenshots'),
    join(playtestDir, 'verify', 'play', 'frames'),
    join(playtestDir, RECORDING_DIR),
  ]);
  if (latestCriticInput && statSync(criticPath).mtimeMs < latestCriticInput) {
    fail('critic-report.json is stale relative to its design/asset/verify evidence — re-dispatch critic');
  }
}

// Concept mockups land at design/<screen>.png (the visual contract). When they
// exist, the visual alignment axis is enforced; when absent (no image
// credentials / pure text), it is skipped.
function gatePerformance() {
  // performance-summary.json only comes from the recording pipeline — N/A when
  // verify.skip_video disables it.
  if (videoVerifySkipped()) return;
  // Recordings are per-scenario, so there can be several performance-summary.json
  // files (verify/recording/<scenario>/). EVERY scenario must pass — a bad-perf
  // scenario can't hide behind a good one.
  const summaryPaths = listFiles(
    playtestDir,
    (_p, name) => name === 'performance-summary.json',
  );
  if (summaryPaths.length === 0) {
    fail('performance-summary.json missing — run record_with_state to capture performance metrics');
    return;
  }

  const gcaConfig = readGcaConfig();
  const cfg = gcaConfig.gates?.performance || {};
  const minFpsP50 = Number(cfg.min_fps_p50 ?? 24);
  const minFpsMin = Number(cfg.min_fps_min ?? 10);
  const maxBootSeconds = Number(cfg.max_boot_seconds ?? 10);
  const maxLongTaskCount = Number(cfg.max_long_task_count ?? 20);
  const maxLongTaskTotalMs = Number(cfg.max_long_task_total_ms ?? 2000);
  const maxMemoryGrowthMb = Number(cfg.max_memory_growth_mb ?? 50);
  const maxObjectGrowthCount = Number(cfg.max_object_growth_count ?? 250);
  const maxObjectSlope = Number(cfg.max_object_slope_count_per_second ?? 10);
  const srcM = latestSrcMtime();

  // Scenario label: the dir name under verify/recording/ (or "recording" if flat).
  const scenarioOf = (p) => {
    const m = p.split(`${RECORDING_DIR}/`)[1];
    return m ? m.split('/')[0] : 'recording';
  };

  for (const summaryPath of summaryPaths) {
    const s = scenarioOf(summaryPath);
    const perf = readJSON(summaryPath);
    if (!perf) { fail(`[${s}] performance-summary.json is invalid JSON`); continue; }

    const fpsP50 = perf.fps?.p50;
    const fpsMin = perf.fps?.min;
    const bootSeconds = perf.timings?.boot_seconds;
    const longTaskCount = perf.long_tasks?.count ?? 0;
    const longTaskTotalMs = perf.long_tasks?.total_duration_ms ?? 0;
    const memoryGrowthMb = perf.memory?.growth_mb;
    const objectGrowth = perf.objects?.growth_count;
    const objectSlope = perf.objects?.slope_count_per_second;

    if (!isFiniteNumber(fpsP50)) {
      fail(`[${s}] performance-summary.json has no fps.p50`);
    } else if (fpsP50 < minFpsP50) {
      fail(`[${s}] fps.p50 ${fpsP50.toFixed(1)} < ${minFpsP50}`);
    }

    if (!isFiniteNumber(fpsMin)) {
      fail(`[${s}] performance-summary.json has no fps.min`);
    } else if (fpsMin < minFpsMin) {
      fail(`[${s}] fps.min ${fpsMin.toFixed(1)} < ${minFpsMin}`);
    }

    if (!isFiniteNumber(bootSeconds)) {
      fail(`[${s}] performance-summary.json has no timings.boot_seconds`);
    } else if (bootSeconds > maxBootSeconds) {
      fail(`[${s}] boot_seconds ${bootSeconds.toFixed(2)} > ${maxBootSeconds}`);
    }

    if (longTaskCount > maxLongTaskCount) {
      fail(`[${s}] long_tasks.count ${longTaskCount} > ${maxLongTaskCount}`);
    }
    if (longTaskTotalMs > maxLongTaskTotalMs) {
      fail(`[${s}] long_tasks.total_duration_ms ${longTaskTotalMs.toFixed(0)} > ${maxLongTaskTotalMs}`);
    }

    // Some browsers do not expose performance.memory; skip when unsupported.
    if (perf.memory?.supported && isFiniteNumber(memoryGrowthMb) && memoryGrowthMb > maxMemoryGrowthMb) {
      fail(`[${s}] memory.growth_mb ${memoryGrowthMb.toFixed(1)} > ${maxMemoryGrowthMb}`);
    }

    if (isFiniteNumber(objectGrowth) && objectGrowth > maxObjectGrowthCount) {
      fail(`[${s}] objects.growth_count ${objectGrowth} > ${maxObjectGrowthCount}`);
    }
    if (isFiniteNumber(objectSlope) && objectSlope > maxObjectSlope) {
      fail(`[${s}] objects.slope_count_per_second ${objectSlope.toFixed(2)} > ${maxObjectSlope}`);
    }

    if (srcM && statSync(summaryPath).mtimeMs < srcM) {
      fail(`[${s}] performance-summary.json is stale (older than latest src/ change) — re-run record_with_state`);
    }
  }
}

function visualUnavailableWarn() {
  const report = readJSON(resolveRead(playtestDir, VISUAL_DIFF_REPORT));
  return report?.status === 'unavailable' && report?.aligned === null;
}

function readJsonl(path) {
  try {
    return readFileSync(path, 'utf-8')
      .split(/\r?\n/)
      .filter((line) => line.trim())
      .map((line) => {
        try { return JSON.parse(line); } catch { return null; }
      })
      .filter(Boolean);
  } catch {
    return [];
  }
}

function validIsoTimestamp(value) {
  return typeof value === 'string' && Number.isFinite(Date.parse(value));
}

function countVisualEvidenceRounds(visualReport, maxVisualRounds, pipelineRunId = null) {
  if (Number(visualReport?.schema_version) >= 2) {
    const entries = readJsonl(resolveRead(playtestDir, VISUAL_DIFF_HISTORY));
    const seen = new Set();
    const valid = entries.filter((entry) => {
      if (
        Number(entry?.schema_version) !== 1
        || entry?.source !== 'element-visual-diff.mjs'
        || typeof entry?.run_id !== 'string'
        || !entry.run_id
        || seen.has(entry.run_id)
        || !validIsoTimestamp(entry?.generated_at)
        || !['passed', 'failed'].includes(entry?.status)
        || (pipelineRunId && entry?.pipeline_run_id !== pipelineRunId)
        || (entry.status === 'passed' && entry.aligned !== true)
        || (entry.status === 'failed' && entry.aligned !== false)
      ) return false;
      const coverage = entry.coverage;
      const comparisons = Number(coverage?.compared_global || 0)
        + Number(coverage?.compared_elements || 0);
      if (!coverage || !Number.isFinite(comparisons) || comparisons <= 0) return false;
      seen.add(entry.run_id);
      return true;
    });
    const latest = valid[valid.length - 1];
    if (
      visualReport?.status !== 'unavailable'
      && (!latest || latest.run_id !== visualReport?.run_id)
    ) return 0;
    return Math.min(valid.length, maxVisualRounds);
  }

  // Migration fallback for sidecars written before schema v2 introduced
  // append-only producer history. Bound the caller-supplied value so NaN,
  // negatives, and one-shot round inflation cannot satisfy the gate.
  const legacyRound = Number(visualReport?.round);
  return Number.isInteger(legacyRound)
    && legacyRound >= 0
    && legacyRound <= maxVisualRounds
    ? legacyRound
    : 0;
}

function countGameplayEvidenceRounds(playReport, pipelineRunId = null) {
  const entries = readJsonl(resolveRead(playtestDir, PLAY_HISTORY));
  const seen = new Set();
  const valid = entries.filter((entry) => {
    const scores = entry?.scores;
    const scoreValues = scores && typeof scores === 'object'
      ? Object.values(scores)
      : [];
    const coverage = entry?.coverage;
    const schemaV2 = Number(entry?.schema_version) >= 2;
    const identity = schemaV2 ? entry?.run_id : entry?.ts;
    if (
      !identity
      || seen.has(identity)
      || !validIsoTimestamp(entry?.ts)
      || entry?.build_health !== true
      || (pipelineRunId && entry?.pipeline_run_id !== pipelineRunId)
      || scoreValues.length === 0
      || !scoreValues.every((score) => Number.isFinite(score) && score >= 0 && score <= 1)
      || !coverage
      || !Number.isFinite(coverage.scene_pct)
      || coverage.scene_pct < 0
      || coverage.scene_pct > 1
    ) return false;
    if (
      schemaV2
      && (entry?.source !== 'play-agent.mjs'
        || typeof entry?.run_id !== 'string'
        || !entry.run_id)
    ) return false;
    seen.add(identity);
    return true;
  });
  if (Number(playReport?.schema_version) >= 2) {
    const latest = valid[valid.length - 1];
    if (!latest || latest.run_id !== playReport?.run_id) return 0;
  }
  return valid.length;
}

function gatePolishRounds() {
  // Thresholds are config-driven so they can stay consistent with
  // `pipeline.max_iterations`. A fast pipeline (max_iterations: 1) cannot
  // legitimately reach the old hard-coded 3/2 rounds, which made
  // ready_to_ship unsatisfiable within the iteration budget.
  const gcaConfig = readGcaConfig();
  const cfg = gcaConfig.gates?.polish || {};
  const minVisual = Number(cfg.visual_rounds_min ?? 1);
  const minGameplay = Number(cfg.gameplay_rounds_min ?? 1);
  const maxVisualRounds = Number(gcaConfig.gates?.visual?.max_rounds ?? 2);
  if (!Number.isInteger(minVisual) || minVisual < 0) {
    fail(`gates.polish.visual_rounds_min must be a non-negative integer, got ${cfg.visual_rounds_min}`);
    return;
  }
  if (!Number.isInteger(minGameplay) || minGameplay < 0) {
    fail(`gates.polish.gameplay_rounds_min must be a non-negative integer, got ${cfg.gameplay_rounds_min}`);
    return;
  }
  if (!Number.isInteger(maxVisualRounds) || maxVisualRounds < 1) {
    fail(`gates.visual.max_rounds must be a positive integer, got ${gcaConfig.gates?.visual?.max_rounds}`);
    return;
  }
  if (minVisual > maxVisualRounds) {
    fail(`gates.polish.visual_rounds_min (${minVisual}) exceeds gates.visual.max_rounds (${maxVisualRounds})`);
    return;
  }
  const requireStress = cfg.require_stress_tested ?? false;
  const pipelineRunId = readJSON(join(playtestDir, 'state.json'))?.pipeline_run_id || null;
  const visualReport = readJSON(resolveRead(playtestDir, VISUAL_DIFF_REPORT));
  const vr = countVisualEvidenceRounds(visualReport, maxVisualRounds, pipelineRunId);
  const playReport = readJSON(resolveRead(playtestDir, PLAY_REPORT));
  const gr = countGameplayEvidenceRounds(playReport, pipelineRunId);
  const formalTests = readJSON(resolveRead(playtestDir, TEST_RESULTS));
  const stressEvidence = (formalTests?.result?.tests || []).some((test) =>
    /^pass(?:ed)?$/i.test(String(test?.status || ''))
      && /stress|soak/i.test(`${test?.name || ''} ${test?.file || ''}`),
  );
  if (vr < minVisual) {
    if (visualUnavailableWarn()) {
      console.warn(`⚠️ visual evidence rounds=${vr} below ${minVisual}, waived because the VLM is unavailable and unavailable_policy=warn.`);
    } else {
      fail(`visual evidence rounds = ${vr}, need ≥ ${minVisual}`);
    }
  }
  if (gr < minGameplay) fail(`gameplay evidence rounds = ${gr}, need ≥ ${minGameplay}`);
  if (requireStress && !stressEvidence) {
    fail('no passing formal stress/soak test evidence was found');
  }
}
// Intelligent play-agent quality gate (Build-Health + objective accuracy +
// freshness + no regressions/blocking bugs). Produced by play-agent.mjs.
function gatePlayQuality() {
  const reportPath = resolveRead(playtestDir, PLAY_REPORT);
  if (!fileExists(reportPath)) {
    fail('agent-play-report.json missing — run the play-agent (game-verify "Agent Play")');
    return;
  }
  const rep = readJSON(reportPath);
  if (!rep) { fail('agent-play-report.json is invalid JSON'); return; }

  if (!rep.build_health?.pass) {
    fail(`play-agent Build-Health failed (booted=${rep.build_health?.booted}, ` +
      `no_fatal_errors=${rep.build_health?.no_fatal_errors}, screenshot=${rep.build_health?.produced_nonempty_screenshot})`);
  }

  const gcaConfig = readGcaConfig();
  const configuredMinAcc = gcaConfig.gates?.play_quality_min_accuracy ?? 0.8;
  const minAcc = Number(args['min-accuracy'] ?? configuredMinAcc);
  if (rep.weighted_accuracy === null || rep.weighted_accuracy === undefined) {
    fail('play-agent produced no weighted_accuracy (no verified objectives) — add objectives.json');
  } else if (rep.weighted_accuracy < minAcc) {
    fail(`play-agent weighted_accuracy ${(rep.weighted_accuracy * 100).toFixed(0)}% < ${(minAcc * 100).toFixed(0)}% threshold`);
  }

  if (rep.regressions?.length) {
    fail(`${rep.regressions.length} objective regression(s) vs previous play run: ${rep.regressions.map(r => r.id).join(', ')}`);
  }

  const blocking = (rep.bugs || []).filter((b) => b.severity === 'crash' || b.source === 'judge');
  if (blocking.length) {
    fail(`${blocking.length} blocking play-agent bug(s) — e.g. "${(blocking[0].message || '').slice(0, 90)}"`);
  }

  // Freshness: report must be newer than the latest src/ change.
  const srcM = latestSrcMtime();
  const repM = rep.generated ? Date.parse(rep.generated) : 0;
  if (srcM && repM && repM < srcM) {
    fail('agent-play-report.json is stale (older than latest src/ change) — re-run the play-agent');
  }
}

export {
  gateVisualVerified,
  gatePlaythroughRecorded,
  gateReleaseWaivers,
  gateVerifyReport,
  gateCriticReport,
  gatePerformance,
  gatePolishRounds,
  gatePlayQuality,
} ;
