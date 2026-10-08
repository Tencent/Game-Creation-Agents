/** Shared constants and helpers for gate domains. */

import {
  readFileSync,
  existsSync,
  readdirSync,
  statSync,
  lstatSync,
  realpathSync,
} from 'fs';

import {
  dirname,
  join,
  resolve,
  sep,
} from 'path';
import { fileURLToPath } from 'node:url';
import {
  resolveRead,
  INTERACTION_CONTRACT,
  LAYOUT_CONTRACT,
} from '../playtest-layout.mjs';
import { readGcaConfigFrom } from '../shared/gca-config.mjs';
import {
  readJSON,
  listFiles,
  runtimeAssetPathIssue,
} from '../shared/gate-io.mjs';
import { computeCanonicalBuildDigest } from '../shared/evidence.mjs';
import {
  gameDir,
  playtestDir,
  configPath,
  fail,
  fileExists,
} from '../gate-context.mjs';

function readGcaConfig() { return readGcaConfigFrom(configPath); }

function canonicalBuildDigest() {
  return computeCanonicalBuildDigest({ gameDir, playtestDir, configPath, manifestEntries });
}

// ── Gates ──────────────────────────────────────────────────────────────
const FILE_LAYOUT_SOURCES = new Set([
  'generated-image',
  'supplied-image',
  'licensed-image',
]);
const RUNTIME_LAYOUT_SOURCES = new Set([
  'procedural',
  'live-text',
  'composition',
]);
const PHASER_BUILTIN_TEXTURE_KEYS = new Set([
  '__DEFAULT',
  '__MISSING',
  '__NORMAL',
  '__WHITE',
]);
const PHASER_RUNTIME_TEXTURE_KEY_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const UI_ASSET_CATEGORIES = new Set([
  'ui', 'hud', 'icon', 'button', 'bar', 'panel', 'badge', 'gauge', 'meter',
  'dashboard', 'toolbar', 'counter', 'widget', 'lettering', 'logo', 'wordmark',
  'art-word', 'frame', 'overlay',
]);
function isUiAssetEntry(key, category) {
  return /^ui[-_]/i.test(String(key || ''))
    || UI_ASSET_CATEGORIES.has(String(category || '').toLowerCase());
}
function isSafeRuntimeAssetPath(value) {
  return runtimeAssetPathIssue(value) === null;
}

function lstatIfPresent(path) {
  try {
    return lstatSync(path);
  } catch (error) {
    if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR') return null;
    throw error;
  }
}

function inspectRuntimeAssetPath(value) {
  const pathIssue = runtimeAssetPathIssue(value);
  if (pathIssue) {
    return { ok: false, exists: false, reason: pathIssue };
  }
  const normalized = String(value);
  const parts = normalized.split('/');
  const assetsRoot = resolve(gameDir, 'assets');
  const candidate = resolve(gameDir, normalized);
  if (candidate === assetsRoot || !candidate.startsWith(`${assetsRoot}${sep}`)) {
    return { ok: false, exists: false, reason: 'path escapes the assets root' };
  }
  try {
    const rootStat = lstatIfPresent(assetsRoot);
    if (!rootStat) return { ok: true, exists: false, path: candidate };
    if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) {
      return {
        ok: false,
        exists: false,
        reason: 'assets root must be a non-symlink directory',
      };
    }
    const canonicalRoot = realpathSync(assetsRoot);
    let current = assetsRoot;
    for (let index = 1; index < parts.length; index += 1) {
      current = join(current, parts[index]);
      const stat = lstatIfPresent(current);
      if (!stat) return { ok: true, exists: false, path: candidate };
      if (stat.isSymbolicLink()) {
        return {
          ok: false,
          exists: false,
          reason: `path component '${parts.slice(0, index + 1).join('/')}' is a symlink`,
        };
      }
      const final = index === parts.length - 1;
      if (final ? !stat.isFile() : !stat.isDirectory()) {
        return {
          ok: false,
          exists: false,
          reason: final
            ? 'final target must be a regular file'
            : `path component '${parts.slice(0, index + 1).join('/')}' is not a directory`,
        };
      }
      const canonicalCurrent = realpathSync(current);
      if (
        canonicalCurrent !== canonicalRoot
        && !canonicalCurrent.startsWith(`${canonicalRoot}${sep}`)
      ) {
        return {
          ok: false,
          exists: false,
          reason: 'resolved path escapes the canonical assets root',
        };
      }
    }
    return { ok: true, exists: true, path: candidate };
  } catch {
    return { ok: false, exists: false, reason: 'path cannot be resolved safely' };
  }
}

function collectLayoutSourceContract(layout = readJSON(resolveRead(playtestDir, LAYOUT_CONTRACT))) {
  const runtimeSources = new Set();
  const runtimeNodes = [];
  const fileNodes = [];
  const fileKeys = new Map();
  const logicalKeys = new Map();
  const sceneLogicalKeys = new Map();
  const errors = [];
  const visit = (node, at, sceneName) => {
    if (!node || typeof node !== 'object' || Array.isArray(node)) return;
    const source = String(node.sourceStrategy || '');
    const key = typeof node.key === 'string' ? node.key : '';
    const bind = typeof node.bind === 'string' ? node.bind : '';
    if (FILE_LAYOUT_SOURCES.has(source) || RUNTIME_LAYOUT_SOURCES.has(source)) {
      for (const identity of new Set([key, bind].filter(Boolean))) {
        const sceneIdentity = `${sceneName}\u0000${identity}`;
        const scenePrior = sceneLogicalKeys.get(sceneIdentity);
        if (scenePrior && scenePrior.at !== at) {
          errors.push(
            `${at}: layout logical identity '${identity}' duplicates `
            + `${scenePrior.at} within scene '${sceneName}' `
            + `(sources '${scenePrior.source}' and '${source}')`,
          );
        } else if (!scenePrior) {
          sceneLogicalKeys.set(sceneIdentity, { at, source, scene: sceneName });
        }
        const prior = logicalKeys.get(identity);
        if (prior && prior.source !== source) {
          errors.push(
            `${at}: layout logical identity '${identity}' conflicts with ${prior.at} `
            + `across scenes (sources '${prior.source}' and '${source}')`,
          );
        } else if (!prior) {
          logicalKeys.set(identity, { at, source, scene: sceneName });
        }
      }
    }
    if (FILE_LAYOUT_SOURCES.has(source)) {
      if (!key) errors.push(`${at}: file-backed layout obligation is missing key`);
      else {
        if (!fileKeys.has(key)) fileKeys.set(key, source);
        fileNodes.push({ node, at, source, scene: sceneName });
      }
    } else if (RUNTIME_LAYOUT_SOURCES.has(source)) {
      runtimeSources.add(source);
      runtimeNodes.push({ node, at, source, scene: sceneName });
    } else if (source === 'bitmap-text') {
      errors.push(
        `${at}: sourceStrategy 'bitmap-text' is unsupported until a manifest-backed `
        + 'font atlas contract exists',
      );
    } else {
      errors.push(`${at}: sourceStrategy '${source || '<missing>'}' is invalid`);
    }

    const kind = String(node.kind || '');
    if (at.includes('.children[')) {
      if (
        kind === 'image'
        && !FILE_LAYOUT_SOURCES.has(source)
        && source !== 'procedural'
      ) {
        errors.push(`${at}: image children must use a file-backed or procedural sourceStrategy`);
      }
      if (kind === 'text' && source !== 'live-text') {
        errors.push(`${at}: text children must use sourceStrategy "live-text"`);
      }
      if (kind === 'bitmap-text') {
        errors.push(`${at}: bitmap-text children are unsupported`);
      }
      if (kind === 'image' && !key) errors.push(`${at}: image child requires key`);
      if (
        kind === 'text'
        && (typeof node.bind !== 'string' || !node.bind)
      ) {
        errors.push(`${at}: ${kind} child requires bind`);
      }
    }
    for (const [index, child] of (
      Array.isArray(node.children) ? node.children : []
    ).entries()) {
      visit(child, `${at}.children[${index}]`, sceneName);
    }
  };

  for (const [sceneName, scene] of Object.entries(layout?.scenes || {})) {
    for (const [index, element] of (
      Array.isArray(scene?.elements) ? scene.elements : []
    ).entries()) {
      visit(
        element,
        `layout-contract.scenes.${sceneName}.elements[${index}]`,
        sceneName,
      );
    }
  }
  return {
    runtimeSources,
    runtimeNodes,
    fileNodes,
    fileKeys,
    logicalKeys,
    sceneLogicalKeys,
    errors,
  };
}

function declaredRuntimeSources() {
  return collectLayoutSourceContract().runtimeSources;
}

function validRuntimeSourceContract(manifest) {
  const allowed = new Set(['procedural', 'live-text', 'composition']);
  const declared = declaredRuntimeSources();
  const listed = new Set(
    Array.isArray(manifest?.runtimeSources)
      ? manifest.runtimeSources.map((source) => String(source))
      : []
  );
  return (
    Array.isArray(manifest?.runtimeSources)
    && listed.size === manifest.runtimeSources.length
    && listed.size === declared.size
    && manifest.runtimeSources.every(
      (source) => allowed.has(String(source)) && declared.has(String(source))
    )
  );
}

function validNoFileAssetContract(manifest) {
  const layoutSources = collectLayoutSourceContract();
  const state = readJSON(join(playtestDir, 'state.json'));
  return (
    ['responsive', 'pixel_art', 'minimal'].includes(state?.quality_profile)
    && qualityProfileName() !== 'art_directed'
    && manifest
    && !Array.isArray(manifest)
    && Number(manifest.schema_version || 0) >= 2
    && manifest.qualityProfile === qualityProfileName()
    && manifest.fileAssetsRequired === false
    && Array.isArray(manifest.assets)
    && manifest.assets.length === 0
    && Array.isArray(manifest.runtimeSources)
    && manifest.runtimeSources.length > 0
    && layoutSources.fileKeys.size === 0
    && layoutSources.errors.length === 0
    && validRuntimeSourceContract(manifest)
  );
}

function allowedRuntimeTextureKeys(manifest) {
  const allowed = new Set(PHASER_BUILTIN_TEXTURE_KEYS);
  for (const entry of manifestEntries(manifest)) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    for (const key of [entry.key, entry.textureKey]) {
      if (typeof key === 'string' && key) allowed.add(key);
    }
    for (const spec of Object.values(entry.actions || {})) {
      if (typeof spec?.textureKey === 'string' && spec.textureKey) {
        allowed.add(spec.textureKey);
      }
    }
  }
  for (const { node } of collectLayoutSourceContract().runtimeNodes) {
    for (const key of [node?.key, node?.bind]) {
      if (typeof key === 'string' && key) allowed.add(key);
    }
  }
  return allowed;
}

function isAllowedRuntimeTextureKey(key, allowed) {
  return allowed.has(key);
}
function hasConceptMockups() {
  const designDir = join(gameDir, 'design');
  if (!existsSync(designDir)) return false;
  try {
    return readdirSync(designDir).some((f) => f.toLowerCase().endsWith('.png'));
  } catch { return false; }
}

// Double-axis alignment evidence written by verify-agent. Each file is
// { axis, applicable, aligned, issues:[...] }. `aligned:false` blocks (rework);
// `applicable:false` skips; missing means the axis was not run.
function canonicalPipelineReference(...segments) {
  const relativePath = join('.claude', 'skills', 'game-pipeline', 'references', ...segments);
  const scriptRepoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../../..');
  const candidates = [
    join(dirname(configPath), relativePath),
    join(scriptRepoRoot, relativePath),
  ];
  return candidates.find(fileExists) || null;
}
function validateInteractionRouting(contract, context) {
  if (!contract) return;
  const state = readJSON(join(playtestDir, 'state.json'));
  if (!state) {
    fail(`${context}: state.json missing or invalid — cannot cross-check interaction routing`);
    return;
  }
  if (!normalizeGenre(state.genre)) {
    fail(`${context}: state.json genre '${state.genre ?? '<missing>'}' is invalid`);
  }
  if (!INTERACTION_PROFILE_SLUGS.includes(state.interaction_profile)) {
    fail(`${context}: state.json interaction_profile '${state.interaction_profile ?? '<missing>'}' is invalid`);
  }
  if (contract.genre !== state.genre) {
    fail(
      `${context}: interaction-contract genre '${contract.genre ?? '<missing>'}' `
      + `must exactly equal state.json genre '${state.genre ?? '<missing>'}'`,
    );
  }
  if (contract.interaction_profile !== state.interaction_profile) {
    fail(
      `${context}: interaction-contract interaction_profile `
      + `'${contract.interaction_profile ?? '<missing>'}' must exactly equal `
      + `state.json interaction_profile '${state.interaction_profile ?? '<missing>'}'`,
    );
  }
}
function isNonEmptyRecord(value) {
  return (
    value !== null
    && typeof value === 'object'
    && !Array.isArray(value)
    && Object.keys(value).length > 0
  );
}

function hasConsumableStagePayload(stage) {
  const contentData = stage?.content?.data;
  return (
    (Array.isArray(stage?.terrain) && stage.terrain.length > 0)
    || (Array.isArray(stage?.spawns) && stage.spawns.length > 0)
    || isNonEmptyRecord(stage?.boss)
    || isNonEmptyRecord(stage?.map_layout)
    || (typeof stage?.map_layout_ref === 'string' && stage.map_layout_ref.length > 0)
    || isNonEmptyRecord(stage?.starter_kit)
    || (
      typeof stage?.content?.kind === 'string'
      && stage.content.kind.trim().length > 0
      && isNonEmptyRecord(contentData)
    )
  );
}

function resolveEffectiveStageMapLayout(levelsDir, stageName, stage) {
  if (Object.hasOwn(stage || {}, 'map_layout')) {
    const layout = stage?.map_layout;
    return {
      layout: isNonEmptyRecord(layout) ? layout : null,
      source: `levels/${stageName}#map_layout`,
      invalid: !isNonEmptyRecord(layout),
      missing: false,
    };
  }

  if (Object.hasOwn(stage || {}, 'map_layout_ref')) {
    const ref = stage?.map_layout_ref;
    if (typeof ref !== 'string' || !ref.trim()) {
      return {
        layout: null,
        source: `levels/${stageName}#map_layout_ref`,
        invalid: true,
        missing: false,
      };
    }
    const path = resolve(gameDir, ref);
    if (path !== gameDir && !path.startsWith(gameDir + sep)) {
      return { layout: null, source: ref, invalid: true, missing: false, unsafe: true };
    }
    const layout = readJSON(path);
    return {
      layout,
      source: ref,
      invalid: fileExists(path) && layout === null,
      missing: !fileExists(path),
    };
  }

  const path = join(levelsDir, 'map-layout.json');
  const layout = readJSON(path);
  return {
    layout,
    source: 'levels/map-layout.json',
    invalid: fileExists(path) && layout === null,
    missing: !fileExists(path),
  };
}
function latestSrcMtime() {
  const srcDir = join(gameDir, 'src');
  let latest = 0;
  function walk(d) {
    if (!existsSync(d)) return;
    for (const e of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, e.name);
      if (e.isDirectory()) walk(p);
      else { try { const m = statSync(p).mtimeMs; if (m > latest) latest = m; } catch {} }
    }
  }
  walk(srcDir);
  return latest;
}

function latestInputMtime(paths) {
  let latest = 0;
  for (const p of paths) {
    if (!existsSync(p)) continue;
    try {
      const stat = statSync(p);
      if (stat.isDirectory()) {
        for (const file of listFiles(p)) {
          const m = statSync(file).mtimeMs;
          if (m > latest) latest = m;
        }
      } else if (stat.mtimeMs > latest) {
        latest = stat.mtimeMs;
      }
    } catch { /* a concurrently replaced input is checked again next run */ }
  }
  return latest;
}

const QUALITY_PROFILES = {
  art_directed: {
    cropCoverage: 'all',
    requireWidgetArt: true,
    nativeUiSize: true,
    integerUiScale: false,
    requirePerSceneConcept: true,
    requireConceptReference: true,
    forbidFxSheets: true,
  },
  responsive: {
    cropCoverage: 'required',
    requireWidgetArt: false,
    nativeUiSize: false,
    integerUiScale: false,
    requirePerSceneConcept: false,
    requireConceptReference: true,
    forbidFxSheets: false,
  },
  pixel_art: {
    cropCoverage: 'required',
    requireWidgetArt: false,
    nativeUiSize: false,
    integerUiScale: true,
    requirePerSceneConcept: false,
    requireConceptReference: true,
    forbidFxSheets: false,
  },
  minimal: {
    cropCoverage: 'required',
    requireWidgetArt: false,
    nativeUiSize: false,
    integerUiScale: false,
    requirePerSceneConcept: false,
    requireConceptReference: true,
    forbidFxSheets: false,
  },
};

const VALID_SCENE_WAIVER_REFERENCES = new Set([
  'waived:pure-transition',
  'waived:concept-generation-unavailable',
]);

function qualityProfileName() {
  const state = readJSON(join(playtestDir, 'state.json'));
  return String(
    state?.quality_profile
    || readGcaConfig().quality?.default_profile
    || 'art_directed',
  ).toLowerCase();
}

function qualityPolicy() {
  const profile = qualityProfileName();
  const policy = QUALITY_PROFILES[profile];
  if (!policy) {
    fail(
      `quality_profile '${profile}' is invalid; expected one of `
      + Object.keys(QUALITY_PROFILES).join(', '),
    );
    return QUALITY_PROFILES.art_directed;
  }
  return policy;
}

function sceneWaiverState(contract) {
  const scenes = contract?.scenes && typeof contract.scenes === 'object'
    ? contract.scenes
    : {};
  const listed = Array.isArray(contract?.waived_scenes)
    ? contract.waived_scenes
    : [];
  const valid = new Set();
  const issues = [];
  for (const sceneKey of new Set(listed)) {
    if (!Object.prototype.hasOwnProperty.call(scenes, sceneKey)) continue;
    const reference = scenes[sceneKey]?.reference;
    if (!VALID_SCENE_WAIVER_REFERENCES.has(reference)) {
      issues.push(
        `layout-contract scene '${sceneKey}' is listed in waived_scenes but its `
        + `reference '${reference ?? '<missing>'}' is not an allowed exact waiver `
        + `(${[...VALID_SCENE_WAIVER_REFERENCES].join(' or ')})`,
      );
      continue;
    }
    valid.add(sceneKey);
  }
  return { valid, issues };
}

function configInputPaths() {
  return [configPath];
}

function latestTestInputMtime() {
  return latestInputMtime([
    join(gameDir, 'src'),
    join(gameDir, 'index.html'),
    join(gameDir, 'assets'),
    join(gameDir, 'levels'),
    join(playtestDir, 'tests'),
    join(playtestDir, 'inputs'),
    ...configInputPaths(),
  ]);
}

function latestVisualInputMtime() {
  return latestInputMtime([
    join(gameDir, 'src'),
    join(gameDir, 'assets'),
    join(gameDir, 'design'),
    resolveRead(playtestDir, LAYOUT_CONTRACT),
    ...configInputPaths(),
  ]);
}

function latestLayoutInputMtime() {
  return latestInputMtime([
    join(gameDir, 'src'),
    join(gameDir, 'assets'),
    join(gameDir, 'design'),
    resolveRead(playtestDir, LAYOUT_CONTRACT),
    ...configInputPaths(),
  ]);
}
// ── UI proportion / per-element art gates (the "proportion control" + per-element mmgdd axis) ──
// Sibling of gateEntityScale, but for HUD/UI images. The size-accurate concept
// mockup already measured every element (ground_crop wrote each crop at its
// box_px); these gates make those measurements the FORWARD contract and block a
// build whose UI was guessed, shipped at the raw generated texture size, or
// rendered as a crude primitive instead of the designed art.

// §1 canvas — every UI proportion is relative to it. CONFIG.screen first, GDD §1 next.
function readCanvasSize() {
  const cfgPath = join(gameDir, 'src/config.js');
  if (fileExists(cfgPath)) {
    try {
      const t = readFileSync(cfgPath, 'utf-8');
      const m = t.match(/screen\s*:\s*\{[^}]*?\bwidth\s*:\s*(\d+)[^}]*?\bheight\s*:\s*(\d+)/);
      if (m) return { w: Number(m[1]), h: Number(m[2]) };
      const m2 = t.match(/screen\s*:\s*\{[^}]*?\bheight\s*:\s*(\d+)[^}]*?\bwidth\s*:\s*(\d+)/);
      if (m2) return { w: Number(m2[2]), h: Number(m2[1]) };
    } catch { /* ignore */ }
  }
  const gddPath = join(gameDir, 'GAME_DESIGN.md');
  if (fileExists(gddPath)) {
    try {
      const t = readFileSync(gddPath, 'utf-8');
      const m = t.match(/Resolution[^\n]*?(\d{3,4})\s*[x×]\s*(\d{3,4})/i);
      if (m) return { w: Number(m[1]), h: Number(m[2]) };
    } catch { /* ignore */ }
  }
  return null;
}
const UI_PANEL_RE = /(panel|stats?|dialog|board|shop|inventory|roster|cardframe|card-frame)/i;
const UI_PANEL_ROLE_RE = /\b(panel|dialog|card|board|modal|shop|inventory|roster|dashboard|stats?|results?)\b/i;
const UI_BANNER_ROLE_RE = /\b(banner|logo|title|wordmark|game[-_ ]?over|victory|defeat|splash|background|backdrop|prompt|decor(?:ative|ation)?|lettering|ornament|flourish|frame|overlay)\b/i;
const UI_HUD_ROLE_RE = /\b(hud|health|energy|mana|stamina|score|bar|gauge|button|btn|badge|icon|minimap|counter|meter|slot|toolbar|fill|track)\b/i;
const UI_HUD_IDENTITY_RE = /(health|energy|mana|stamina|score|counter|bar|gauge|button|btn|badge|icon|minimap|meter|slot|toolbar)/i;
// Banner = full-screen lettering/backdrops (lenient size cap). Deliberately does
// NOT match a `-bg`/`_bg` SUFFIX: those are widget frame art (ui-health-bg,
// ui-panel-bg) — HUD/panel elements, not screen banners — and must keep the
// stricter cap so an unscaled bar/panel frame is still flagged.
const UI_BANNER_RE = /(logo|title|wordmark|gameover|game[-_]?over|victory|defeat|banner|splash|background)/i;
function uiExplicitRoleBucket(explicitRole = '') {
  const role = String(explicitRole || '').replace(/[-_]+/g, ' ').trim().toLowerCase();
  if (UI_HUD_ROLE_RE.test(role)) return 'hud';
  if (UI_PANEL_ROLE_RE.test(role)) return 'panel';
  if (UI_BANNER_ROLE_RE.test(role)) return 'banner';
  return null;
}
function uiIdentityBucket(key, category) {
  const k = String(key || '').toLowerCase();
  const c = String(category || '').toLowerCase();
  if (
    UI_HUD_IDENTITY_RE.test(k)
    || ['hud', 'button', 'badge', 'gauge', 'meter', 'counter', 'icon', 'toolbar'].includes(c)
  ) return 'hud';
  if (
    UI_PANEL_RE.test(k)
    || ['panel', 'dashboard', 'board', 'dialog'].includes(c)
  ) return 'panel';
  if (UI_BANNER_RE.test(k) || c === 'background' || c === 'banner') return 'banner';
  return null;
}
function uiRoleResolution(key, category, explicitRole = '') {
  const roleBucket = uiExplicitRoleBucket(explicitRole);
  const identityBucket = uiIdentityBucket(key, category);
  const unsafeBanner = (
    roleBucket === 'banner'
    && (identityBucket === 'hud' || identityBucket === 'panel')
  );
  return {
    roleBucket,
    identityBucket,
    unsafeBanner,
    bucket: unsafeBanner
      ? identityBucket
      : (roleBucket || identityBucket || 'hud'),
  };
}
function uiRoleBucket(key, category, explicitRole = '') {
  return uiRoleResolution(key, category, explicitRole).bucket;
}

// Functional HUD-widget labels (structures that are ART seeded from a crop), vs
// pure dynamic VALUES (the changing number itself, which is always runtime text).
function isFunctionalWidgetLabel(s) {
  const t = String(s || '').replace(/[-_]/g, ' ');
  return /\b(widget|control|health|energy|mana|stamina|score|bar|button|btn|panel|badge|gauge|meter|hud|icon|slot|minimap|portrait|avatar|toolbar|counter|display|heart|coin|gem|star)\b/i.test(t);
}
function isFunctionalUiSemantics(
  key,
  category,
  explicitRole = '',
  functionalContext = '',
) {
  const { roleBucket, identityBucket } = uiRoleResolution(
    key,
    category,
    explicitRole,
  );
  return (
    roleBucket === 'hud'
    || roleBucket === 'panel'
    || identityBucket === 'hud'
    || identityBucket === 'panel'
    || isFunctionalWidgetLabel([key, category, explicitRole].filter(Boolean).join(' '))
    || isFunctionalWidgetLabel(functionalContext)
  );
}
function uiAllowsLenientExemption(
  key,
  category,
  explicitRole = '',
  decorative = false,
  functionalContext = '',
) {
  if (
    isFunctionalUiSemantics(
      key,
      category,
      explicitRole,
      functionalContext,
    )
  ) return false;
  return decorative === true
    || uiRoleBucket(key, category, explicitRole) === 'banner';
}
const DISPLAY_PARAM_RESERVED = new Set([
  'schema_version', 'version', 'generated_at', 'notes', 'uiCanvas', 'ui',
]);

function normalizeDisplayParams(dp) {
  if (!dp || typeof dp !== 'object') return null;
  if (dp.entities && typeof dp.entities === 'object' && !Array.isArray(dp.entities)) {
    return dp.entities;
  }
  if (Array.isArray(dp.entities)) {
    const out = {};
    for (const it of dp.entities) {
      const key = it && (it.key || it.name || it.id);
      if (key) out[key] = it;
    }
    return out;
  }
  const out = {};
  for (const [key, value] of Object.entries(dp)) {
    if (DISPLAY_PARAM_RESERVED.has(key)) continue;
    if (value && typeof value === 'object' && !Array.isArray(value)) out[key] = value;
  }
  return out;
}

function bodySpaceOf(entity) {
  return entity && ['frame', 'world'].includes(entity.bodySpace)
    ? entity.bodySpace
    : null;
}

function worldBodyDimensions(entity) {
  if (!entity || !Number.isFinite(entity.bodyWidth) || !Number.isFinite(entity.bodyHeight)) return null;
  const bodySpace = bodySpaceOf(entity);
  if (!bodySpace) return null;
  const factor = bodySpace === 'frame'
    ? (Number.isFinite(entity.scale) && entity.scale > 0 ? entity.scale : NaN)
    : 1;
  if (!Number.isFinite(factor)) return null;
  return { w: entity.bodyWidth * factor, h: entity.bodyHeight * factor };
}

function worldVisibleDimensions(entity) {
  const bbox = entity?.contentBbox;
  if (!bbox || !Number.isFinite(bbox.w) || !Number.isFinite(bbox.h) ||
      !Number.isFinite(entity.scale) || entity.scale <= 0) return null;
  return { w: bbox.w * entity.scale, h: bbox.h * entity.scale };
}
// 10 gameplay genres + the `custom` engineering fallback.
const GENRE_SLUGS = ['platform', 'beat_em_up', 'bullet_hell', 'roguelike', 'puzzle', 'tower_defence', 'sandbox', 'simulation', 'card', 'idle', 'custom'];
const INTERACTION_PROFILE_SLUGS = ['arcade-gravity', 'arcade-no-gravity', 'matter', 'none'];
function normalizeGenre(v) {
  if (typeof v !== 'string') return null;
  const s = v.toLowerCase().trim();
  return GENRE_SLUGS.includes(s) ? s : null;
}

let _genreCache;
function detectGenre() {
  if (_genreCache !== undefined) return _genreCache;
  const state = readJSON(join(playtestDir, 'state.json'));
  _genreCache = normalizeGenre(state?.genre);
  return _genreCache;
}
// Physics seed from Startup routing metadata, recorded in state.json — the authoritative
// disambiguator for one-to-many genres (e.g. a matter `puzzle`).
function detectInteractionProfile() {
  const state = readJSON(join(playtestDir, 'state.json'));
  if (state && typeof state.interaction_profile === 'string') return state.interaction_profile.toLowerCase();
  return null;
}
function isTowerDefence() { return detectGenre() === 'tower_defence'; }
function usesAuthoredLevels() {
  const state = readJSON(join(playtestDir, 'state.json'));
  return state?.level_model === 'authored';
}
// Physics applicability is an authored contract. Genre is intentionally not a
// proxy: e.g. a card-like simulation may have no bodies, while a custom game may.
function isPhysicsGame() {
  const ip = detectInteractionProfile();
  if (ip === 'none') return false;
  if (ip === 'arcade-gravity' || ip === 'arcade-no-gravity' || ip === 'matter') return true;
  const contract = readJSON(resolveRead(playtestDir, INTERACTION_CONTRACT));
  const rows = Array.isArray(contract?.matrix) ? contract.matrix : [];
  return rows.some((row) =>
    Array.isArray(row)
    && ['collide', 'collide_oneway', 'overlap'].includes(row[2])
  );
}
function tdConfig() { const g = readGcaConfig().gates || {}; return g.tower_defence || {}; }

function readSrcText() {
  const files = listFiles(join(gameDir, 'src'), (_p, n) => n.endsWith('.js'));
  let out = '';
  for (const f of files) { try { out += `\n/*${f}*/\n` + readFileSync(f, 'utf-8'); } catch { /* ignore */ } }
  return out;
}

function stripJsComments(text) {
  let out = '';
  let mode = 'code';
  let quote = '';
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1];
    if (mode === 'line') {
      if (ch === '\n') { mode = 'code'; out += ch; }
      continue;
    }
    if (mode === 'block') {
      if (ch === '*' && next === '/') { mode = 'code'; i += 1; }
      else if (ch === '\n') out += ch;
      continue;
    }
    if (mode === 'string') {
      out += ch;
      if (ch === '\\') {
        if (i + 1 < text.length) out += text[++i];
      } else if (ch === quote) {
        mode = 'code';
      }
      continue;
    }
    if (ch === '/' && next === '/') { mode = 'line'; i += 1; continue; }
    if (ch === '/' && next === '*') { mode = 'block'; i += 1; continue; }
    if (ch === '"' || ch === "'" || ch === '`') {
      mode = 'string';
      quote = ch;
    }
    out += ch;
  }
  return out;
}
function manifestEntries(manifest) {
  if (!manifest) return [];
  if (Array.isArray(manifest)) return manifest;
  for (const field of ['assets', 'entries', 'items']) {
    if (Array.isArray(manifest[field])) return manifest[field];
  }

  // Normalize legacy nested maps (`sprites.<key>`, `props.<pack>.items.<key>`,
  // etc.) to the same flat entry shape consumed by every gate.
  const out = [];
  const reserved = new Set([
    'schema_version', 'version', 'generated_at', 'notes',
    'qualityProfile', 'fileAssetsRequired', 'runtimeSources',
  ]);
  const singularCategory = (value) => ({
    sprites: 'sprite',
    characters: 'character',
    enemies: 'enemy',
    bosses: 'boss',
    npcs: 'npc',
    projectiles: 'projectile',
    props: 'prop',
    tiles: 'tile',
    tilesets: 'tileset',
    platforms: 'platform',
    obstacles: 'obstacle',
    backgrounds: 'background',
  }[String(value || '').toLowerCase()] || value);
  const visit = (node, category, keyHint) => {
    category = singularCategory(category);
    if (node === null || node === undefined) return;
    if (typeof node === 'string') {
      if (keyHint) out.push({ key: keyHint, category, path: node });
      return;
    }
    if (Array.isArray(node)) {
      node.forEach((v, i) => visit(v, category, v?.key || `${keyHint || category}-${i}`));
      return;
    }
    if (typeof node !== 'object') return;
    const key = node.key || keyHint;
    if (node.path || node.actions || node.textureKey) {
      out.push({ ...node, key, category: singularCategory(node.category || category) });
      return;
    }
    if (node.items && typeof node.items === 'object') {
      for (const [k, v] of Object.entries(node.items)) visit(v, category, k);
      return;
    }
    for (const [k, v] of Object.entries(node)) {
      if (!reserved.has(k)) visit(v, category, k);
    }
  };

  for (const [category, node] of Object.entries(manifest)) {
    if (!reserved.has(category)) visit(node, category, null);
  }
  return out;
}

export {
  readGcaConfig,
  canonicalBuildDigest,
  FILE_LAYOUT_SOURCES,
  RUNTIME_LAYOUT_SOURCES,
  PHASER_BUILTIN_TEXTURE_KEYS,
  PHASER_RUNTIME_TEXTURE_KEY_RE,
  UI_ASSET_CATEGORIES,
  isUiAssetEntry,
  isSafeRuntimeAssetPath,
  lstatIfPresent,
  inspectRuntimeAssetPath,
  collectLayoutSourceContract,
  declaredRuntimeSources,
  validRuntimeSourceContract,
  validNoFileAssetContract,
  allowedRuntimeTextureKeys,
  isAllowedRuntimeTextureKey,
  hasConceptMockups,
  canonicalPipelineReference,
  validateInteractionRouting,
  isNonEmptyRecord,
  hasConsumableStagePayload,
  resolveEffectiveStageMapLayout,
  latestSrcMtime,
  latestInputMtime,
  QUALITY_PROFILES,
  VALID_SCENE_WAIVER_REFERENCES,
  qualityProfileName,
  qualityPolicy,
  sceneWaiverState,
  configInputPaths,
  latestTestInputMtime,
  latestVisualInputMtime,
  latestLayoutInputMtime,
  readCanvasSize,
  UI_PANEL_RE,
  UI_PANEL_ROLE_RE,
  UI_BANNER_ROLE_RE,
  UI_HUD_ROLE_RE,
  UI_HUD_IDENTITY_RE,
  UI_BANNER_RE,
  uiExplicitRoleBucket,
  uiIdentityBucket,
  uiRoleResolution,
  uiRoleBucket,
  isFunctionalWidgetLabel,
  isFunctionalUiSemantics,
  uiAllowsLenientExemption,
  DISPLAY_PARAM_RESERVED,
  normalizeDisplayParams,
  bodySpaceOf,
  worldBodyDimensions,
  worldVisibleDimensions,
  GENRE_SLUGS,
  INTERACTION_PROFILE_SLUGS,
  normalizeGenre,
  detectGenre,
  detectInteractionProfile,
  isTowerDefence,
  usesAuthoredLevels,
  isPhysicsGame,
  tdConfig,
  readSrcText,
  stripJsComments,
  manifestEntries,
} ;
