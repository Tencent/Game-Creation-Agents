/** Asset contract gates — mechanically extracted gate domain. */

import {
  readFileSync,
  existsSync,
  readdirSync,
  statSync,
  lstatSync,
  realpathSync,
  writeFileSync,
  mkdirSync,
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
  ASSET_INTEGRATION_REPORT,
  DISPLAY_PARAMS,
  SLOT_VALIDATION_DIR,
  SCALE_VALIDATION_DIR,
  SCALE_REPORT,
} from '../playtest-layout.mjs';

import {
  readJSON,
  sha256File,
  slotContractSha256,
  gddSlotContextSha256,
  pngSize,
  pngInfo,
  listFiles,
  canonicalRelativeBrowserPathIssue,
} from '../shared/gate-io.mjs';
import {
  gameDir,
  playtestDir,
  configPath,
  fail,
  fileExists,
} from '../gate-context.mjs';
import {
  readGcaConfig,
  isUiAssetEntry,
  isSafeRuntimeAssetPath,
  lstatIfPresent,
  inspectRuntimeAssetPath,
  collectLayoutSourceContract,
  validRuntimeSourceContract,
  validNoFileAssetContract,
  latestInputMtime,
  qualityProfileName,
  qualityPolicy,
  isFunctionalWidgetLabel,
  normalizeDisplayParams,
  bodySpaceOf,
  worldBodyDimensions,
  readSrcText,
  stripJsComments,
  manifestEntries,
} from './support.mjs';

function gateAssetManifest() {
  const manifestPath = join(gameDir, 'assets', 'asset-manifest.json');
  if (!fileExists(manifestPath)) {
    fail('assets/asset-manifest.json missing — scaffold must create the shared asset contract before gameplay/assets run in parallel');
    return null;
  }
  const manifest = readJSON(manifestPath);
  if (!manifest) {
    fail('assets/asset-manifest.json is invalid JSON');
    return null;
  }
  const entries = manifestEntries(manifest);
  const layoutSources = collectLayoutSourceContract();
  for (const error of layoutSources.errors) {
    fail(`layout source contract: ${error}`);
  }
  if (entries.length === 0) {
    if (layoutSources.fileKeys.size > 0) {
      fail(
        'asset-manifest cannot use the no-file contract while layout-contract '
        + `declares file-backed keys: ${[...layoutSources.fileKeys.keys()].join(', ')}`,
      );
    }
    if (!validNoFileAssetContract(manifest)) {
      fail(
        'asset-manifest has no file assets without a valid non-art profile '
        + 'fileAssetsRequired:false + runtimeSources contract'
      );
    }
    return manifest;
  }

  const spriteCats = new Set(['sprite', 'character', 'enemy', 'boss', 'npc', 'projectile']);
  const strictSources = Number(manifest.schema_version || 0) >= 2;
  const fileSources = new Set(['generated-image', 'supplied-image', 'licensed-image']);
  const routingState = readJSON(join(playtestDir, 'state.json'));
  if (
    strictSources
    && !['art_directed', 'responsive', 'pixel_art', 'minimal']
      .includes(routingState?.quality_profile)
  ) {
    fail('schema-v2 asset-manifest requires a valid state.json quality_profile');
  }
  if (strictSources && manifest.qualityProfile !== qualityProfileName()) {
    fail('schema-v2 asset-manifest requires qualityProfile matching state.json');
  }
  if (strictSources && manifest.fileAssetsRequired !== true) {
    fail('schema-v2 asset-manifest with file entries requires fileAssetsRequired:true');
  }
  if (strictSources && !validRuntimeSourceContract(manifest)) {
    fail('asset-manifest runtimeSources do not match layout-contract source strategies');
  }
  const profile = qualityProfileName();
  if (strictSources && profile === 'art_directed') {
    for (const { node, at, source } of layoutSources.runtimeNodes) {
      const kind = String(node.kind || '').toLowerCase();
      const role = String(node.role || '');
      const key = String(node.key || node.bind || '');
      const dynamicText = (
        source === 'live-text'
        && typeof node.bind === 'string'
        && node.bind
      );
        const layoutOnlyCluster = (
          source === 'composition'
          && kind === 'cluster'
          && Array.isArray(node.children)
        );
        // An unbound zone is a region, not a widget: it renders no pixels of its
        // own, so requiring it to ship as art asks for art nobody can ever see.
        // isFunctionalWidgetLabel reads the key alone, so a sensor named for what
        // it measures is caught by the bare word -- flappy-bird's `score-gate`,
        // and equally any star/coin/heart trigger, since those are in the list too.
        const unboundProceduralZone = (
          source === 'procedural'
          && kind === 'zone'
          && !node.bind
        );
        if (
          source === 'live-text'
          && !dynamicText
        ) {
          fail(`${at}: art_directed fixed text must use image art; runtime text requires a bind`);
        } else if (
          !dynamicText
          && !layoutOnlyCluster
          && !unboundProceduralZone
          && isFunctionalWidgetLabel(`${key} ${role}`)
        ) {
        fail(`${at}: art_directed functional widget '${key || role}' cannot use runtime source '${source}'`);
      }
    }
  }
  const manifestByKey = new Map(
    entries
      .filter((entry) => entry && typeof entry === 'object' && !Array.isArray(entry))
      .map((entry) => [String(entry.key || ''), entry]),
  );
  for (const [key, source] of layoutSources.fileKeys) {
    const entry = manifestByKey.get(key);
    if (!entry) {
      fail(`file-backed layout key '${key}' is missing its asset-manifest row`);
    } else if (strictSources && entry.sourceStrategy !== source) {
      fail(
        `file-backed layout key '${key}' sourceStrategy '${source}' does not `
        + `match asset-manifest '${entry.sourceStrategy ?? '<missing>'}'`,
      );
    }
  }
  const textureKeys = new Set();
  const logicalKeys = new Set();
  const claimTextureKey = (textureKey, owner) => {
    if (typeof textureKey !== 'string' || !textureKey) return;
    if (textureKeys.has(textureKey)) {
      fail(`asset-manifest duplicates Phaser texture key '${textureKey}' (at '${owner}')`);
    }
    textureKeys.add(textureKey);
  };
  for (const entry of entries) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      fail(`asset-manifest entries must be objects, got ${JSON.stringify(entry)}`);
      continue;
    }
    const key = String(entry.key || '');
    if (!key) {
      fail('asset-manifest.json contains an entry with no logical key');
      continue;
    }
    const category = String(entry.category || '').toLowerCase();
    const isUi = isUiAssetEntry(key, category);
    if (strictSources) {
      const source = String(entry.sourceStrategy || '');
      if (!fileSources.has(source)) {
        fail(`asset-manifest '${key}' has invalid file sourceStrategy '${source}'`);
      }
      if (
        (source === 'supplied-image' || source === 'licensed-image')
        && !entry.provenance
      ) {
        fail(`asset-manifest '${key}' ${source} is missing provenance`);
      }
      if (
        source === 'licensed-image'
        && !entry.provenance?.licenseId
      ) {
        fail(`asset-manifest '${key}' licensed-image provenance is missing licenseId`);
      }
      if (
        spriteCats.has(String(entry.category || '').toLowerCase())
        && source !== 'generated-image'
      ) {
        fail(
          `asset-manifest sprite '${key}' uses unsupported ${source}; `
          + 'validated existing-sprite ingestion is not declared'
        );
      }
      if (typeof entry.description !== 'string' || !entry.description.trim()) {
        fail(`asset-manifest '${key}' is missing its canonical GDD description`);
      }
      if (isUi) {
        if (
          entry.decorative === true
          && isFunctionalWidgetLabel(
            [key, category, entry.description].filter(Boolean).join(' '),
          )
        ) {
          fail(`asset-manifest functional UI '${key}' cannot claim decorative:true`);
        }
        const present = (fields) => fields.filter((field) => Object.hasOwn(entry, field));
        const positivePair = (value) => (
          Array.isArray(value)
          && value.length === 2
          && value.every((item) => Number.isInteger(item) && item > 0)
        );
        if (profile === 'art_directed') {
          const targetFields = present(['targetWidth', 'targetHeight']);
          if (
            targetFields.length !== 1
            || !Number.isInteger(entry[targetFields[0]])
            || entry[targetFields[0]] <= 0
          ) {
            fail(
              `asset-manifest UI '${key}' must declare exactly targetWidth xor `
              + 'targetHeight as a positive integer for art_directed',
            );
          }
          const forbidden = present([
            'maxBox', 'logicalBox', 'baseSize', 'integerScales',
          ]);
          if (forbidden.length > 0) {
            fail(
              `asset-manifest UI '${key}' cannot declare ${forbidden.join(', ')} `
              + 'for art_directed sizing',
            );
          }
        } else if (profile === 'responsive' || profile === 'minimal') {
          const fitFields = present(['maxBox', 'logicalBox']);
          if (
            fitFields.length !== 1
            || !positivePair(entry[fitFields[0]])
          ) {
            fail(
              `asset-manifest UI '${key}' requires logicalBox/maxBox (exactly `
              + `one positive pair) for ${profile} uniform fit`,
            );
          }
          const forbidden = present([
            'targetWidth', 'targetHeight', 'baseSize', 'integerScales',
          ]);
          if (forbidden.length > 0) {
            fail(
              `asset-manifest UI '${key}' cannot declare ${forbidden.join(', ')} `
              + `for ${profile} sizing`,
            );
          }
        } else if (profile === 'pixel_art') {
          const pixelSized = (
            positivePair(entry.baseSize)
            && Array.isArray(entry.integerScales)
            && entry.integerScales.length > 0
            && entry.integerScales.every(
              (scale) => Number.isInteger(scale) && scale > 0,
            )
          );
          if (!pixelSized) {
            fail(
              `asset-manifest UI '${key}' requires baseSize + positive `
              + 'integerScales for pixel_art',
            );
          }
          const forbidden = present([
            'targetWidth', 'targetHeight', 'maxBox', 'logicalBox',
          ]);
          if (forbidden.length > 0) {
            fail(
              `asset-manifest UI '${key}' cannot declare ${forbidden.join(', ')} `
              + 'for pixel_art sizing',
            );
          }
        }
      }
    }
    if (logicalKeys.has(key)) fail(`asset-manifest duplicates logical key '${key}'`);
    logicalKeys.add(key);

    if (!spriteCats.has(String(entry.category || '').toLowerCase())) {
      const textureKey = entry.textureKey || key;
      claimTextureKey(textureKey, key);
      const declaredWidth = entry.width;
      const declaredHeight = entry.height;
      const finalizedDimensions = (
        Number.isInteger(declaredWidth)
        && declaredWidth > 0
        && Number.isInteger(declaredHeight)
        && declaredHeight > 0
      );
      if (typeof entry.path !== 'string' || !entry.path) {
        fail(`asset-manifest static asset '${key}' missing runtime path`);
      } else if (!isSafeRuntimeAssetPath(entry.path)) {
        fail(`asset-manifest static asset '${key}' has unsafe runtime path '${entry.path}'`);
      } else if (!/[.]png$/i.test(entry.path)) {
        fail(`asset-manifest static asset '${key}' runtime path must end in .png`);
      } else {
        const inspected = inspectRuntimeAssetPath(entry.path);
        if (!inspected.ok) {
          fail(
            `asset-manifest static asset '${key}' has unsafe runtime path `
            + `'${entry.path}': ${inspected.reason}`,
          );
        } else if (inspected.exists) {
          if (!finalizedDimensions) {
            fail(
              `asset-manifest static asset '${key}' requires positive finalized `
              + 'integer width and height once its runtime file exists',
            );
          }
          const actual = pngSize(inspected.path);
          if (!actual) {
            fail(`asset-manifest static asset '${key}' runtime file is not a readable PNG`);
          } else {
            if (
              finalizedDimensions
              && (actual.w !== declaredWidth || actual.h !== declaredHeight)
            ) {
              fail(
                `asset-manifest static asset '${key}' declares `
                + `${declaredWidth}×${declaredHeight} but PNG is ${actual.w}×${actual.h}`,
              );
            }
            if (strictSources && isUi) {
              if (profile === 'art_directed') {
                const targetField = Object.hasOwn(entry, 'targetWidth')
                  ? 'targetWidth'
                  : 'targetHeight';
                const actualAxis = targetField === 'targetWidth' ? actual.w : actual.h;
                if (actualAxis !== Number(entry[targetField])) {
                  fail(
                    `asset-manifest art_directed UI '${key}' PNG ${targetField} axis `
                    + `${actualAxis} must equal ${targetField} ${entry[targetField]}`,
                  );
                }
              } else if (profile === 'responsive' || profile === 'minimal') {
                const fitField = Object.hasOwn(entry, 'maxBox') ? 'maxBox' : 'logicalBox';
                const fit = entry[fitField];
                if (
                  Array.isArray(fit)
                  && (actual.w > Number(fit[0]) || actual.h > Number(fit[1]))
                ) {
                  fail(
                    `asset-manifest ${profile} UI '${key}' PNG dimensions `
                    + `${actual.w}×${actual.h} must fit ${fitField} `
                    + `${fit[0]}×${fit[1]}`,
                  );
                }
              } else if (
                profile === 'pixel_art'
                && (
                  actual.w !== Number(entry.baseSize?.[0])
                  || actual.h !== Number(entry.baseSize?.[1])
                )
              ) {
                fail(
                  `asset-manifest pixel_art UI '${key}' PNG dimensions `
                  + `${actual.w}×${actual.h} must equal baseSize `
                  + `${entry.baseSize?.[0]}×${entry.baseSize?.[1]}`,
                );
              }
            }
          }
        }
      }
      continue;
    }

    const actions = entry.actions;
    if (actions && typeof actions === 'object' && !Array.isArray(actions)) {
      const names = Object.keys(actions);
      if (names.length === 0) {
        fail(`asset-manifest sprite '${key}' has an empty actions map`);
        continue;
      }
      const defaultAction = entry.defaultAction;
      if (typeof defaultAction !== 'string' || !actions[defaultAction]) {
        fail(`asset-manifest sprite '${key}' defaultAction must name one declared action`);
      }
      for (const [action, spec] of Object.entries(actions)) {
        if (!spec || typeof spec !== 'object') {
          fail(`asset-manifest sprite '${key}' action '${action}' must be an object`);
          continue;
        }
        for (const field of ['textureKey', 'path', 'metadata']) {
          if (typeof spec[field] !== 'string' || !spec[field]) {
            fail(`asset-manifest sprite '${key}' action '${action}' missing '${field}'`);
          }
        }
        for (const field of ['path', 'metadata']) {
          const rel = spec[field];
          if (typeof rel !== 'string') continue;
          if (!isSafeRuntimeAssetPath(rel)) {
            fail(
              `asset-manifest sprite '${key}' action '${action}' has unsafe `
              + `${field} path '${rel}'`,
            );
            continue;
          }
          const inspected = inspectRuntimeAssetPath(rel);
          if (!inspected.ok) {
            fail(
              `asset-manifest sprite '${key}' action '${action}' has unsafe `
              + `${field} path '${rel}': ${inspected.reason}`,
            );
          }
        }
        claimTextureKey(spec.textureKey, `${key}.${action}`);
        if (typeof spec.path === 'string' && typeof spec.metadata === 'string') {
          const pngBase = spec.path.replace(/\.png$/i, '');
          const jsonBase = spec.metadata.replace(/\.json$/i, '');
          if (pngBase === spec.path || jsonBase === spec.metadata || pngBase !== jsonBase) {
            fail(`asset-manifest sprite '${key}' action '${action}' path/metadata must be same-basename .png/.json`);
          }
        }
      }
      const def = actions[defaultAction];
      if (def && (entry.path !== def.path || entry.metadata !== def.metadata)) {
        fail(`asset-manifest sprite '${key}' top-level path/metadata must mirror defaultAction '${defaultAction}'`);
      }
    } else {
      // Explicit legacy combined layout only. New games must use actions{}.
      claimTextureKey(entry.textureKey || key, key);
      if (entry.layout !== 'combined') {
        fail(`asset-manifest sprite '${key}' must declare canonical actions{} or explicit layout:'combined'`);
      }
      if (typeof entry.path !== 'string' || typeof entry.metadata !== 'string') {
        fail(`legacy combined sprite '${key}' requires declared path + same-basename metadata`);
      } else {
        if (!isSafeRuntimeAssetPath(entry.path) || !isSafeRuntimeAssetPath(entry.metadata)) {
          fail(`legacy combined sprite '${key}' paths must stay under assets/**`);
        } else {
          for (const [field, rel] of [
            ['path', entry.path],
            ['metadata', entry.metadata],
          ]) {
            const inspected = inspectRuntimeAssetPath(rel);
            if (!inspected.ok) {
              fail(
                `legacy combined sprite '${key}' has unsafe ${field} '${rel}': `
                + inspected.reason,
              );
            }
          }
        }
        const pngBase = entry.path.replace(/\.png$/i, '');
        const jsonBase = entry.metadata.replace(/\.json$/i, '');
        const legacyStandalone = /(^|\/)spritesheet-meta\.json$/i.test(entry.metadata);
        if (pngBase === entry.path || jsonBase === entry.metadata ||
            (pngBase !== jsonBase && !legacyStandalone)) {
          fail(`legacy combined sprite '${key}' metadata must be same-basename JSON or explicit spritesheet-meta.json`);
        }
      }
    }
  }
  return manifest;
}

function gateAssetRuntimeReferences() {
  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  if (!manifest) return; // gateAssetManifest reports the structural error
  const srcDir = join(gameDir, 'src');
  if (!fileExists(srcDir)) {
    fail('src/ missing — cannot prove final manifest assets are runtime-referenced');
    return;
  }
  const spriteCats = new Set(['sprite', 'character', 'enemy', 'boss', 'npc', 'projectile']);
  const claims = [];
  for (const entry of manifestEntries(manifest)) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    const key = String(entry.key || '');
    if (!spriteCats.has(String(entry.category || '').toLowerCase())) {
      claims.push({
        owner: key,
        kind: 'static',
        textureKey: entry.textureKey || key,
        path: entry.path,
      });
      continue;
    }
    if (entry.actions && typeof entry.actions === 'object' && !Array.isArray(entry.actions)) {
      for (const [action, spec] of Object.entries(entry.actions)) {
        if (!spec || typeof spec !== 'object') continue;
        claims.push({
          owner: `${key}.${action}`,
          kind: 'sprite',
          textureKey: spec.textureKey,
          path: spec.path,
        });
      }
    } else {
      claims.push({
        owner: key,
        kind: 'sprite',
        textureKey: entry.textureKey || key,
        path: entry.path,
      });
    }
  }

  const srcText = stripJsComments(readSrcText());
  if (hasManifestDrivenRuntimeLoader(srcText, claims)) return;
  const unused = claims.filter((claim) => {
    const basename = typeof claim.path === 'string'
      ? claim.path.split(/[\\/]/).pop()?.replace(/\.[^.]+$/, '')
      : '';
    const keyReferenced = typeof claim.textureKey === 'string'
      && claim.textureKey
      && srcText.includes(claim.textureKey);
    const pathReferenced = [claim.path, basename]
      .filter((value) => typeof value === 'string' && value)
      .some((value) => srcText.includes(value));
    return !keyReferenced || !pathReferenced;
  });
  if (unused.length) {
    fail(
      `asset-manifest has ${unused.length} final runtime asset(s) not referenced `
      + `by source: ${unused.slice(0, 8).map((x) => x.owner).join(', ')}. `
      + `Load every final key/path explicitly; review-only intermediates must stay out of the manifest.`,
    );
  }
}
function gateAssetsGenerated(state) {
  if (!state) return;
  if (!state.assets_generated) fail('assets_generated is false — asset/source phase incomplete');

  // Check if assets directory has actual image files
  const assetsDir = join(gameDir, 'assets');
  const imageExts = ['.png', '.jpg', '.jpeg', '.webp', '.gif'];
  const imagePaths = [];
  const assetsStat = lstatIfPresent(assetsDir);
  if (assetsStat?.isSymbolicLink() || (assetsStat && !assetsStat.isDirectory())) {
    fail('assets root must be a non-symlink directory');
  } else if (assetsStat) {
    function walkImages(d) {
      for (const e of readdirSync(d, { withFileTypes: true })) {
        const path = join(d, e.name);
        if (e.isSymbolicLink()) {
          fail(`asset files must not use symlinks: '${relative(gameDir, path)}'`);
        } else if (e.isDirectory()) walkImages(path);
        else if (imageExts.some(ext => e.name.endsWith(ext))) imagePaths.push(path);
      }
    }
    walkImages(assetsDir);
  }

  const manifest = readJSON(join(assetsDir, 'asset-manifest.json'));
  const noFileAssets = manifestEntries(manifest).length === 0
    && validNoFileAssetContract(manifest);
  if (imagePaths.length === 0 && !noFileAssets) {
    fail('No image files in assets/ directory. Runtime generateTexture/fillRect code is a placeholder, not a generated asset. Use /game-assets to produce real image files.');
  }

  // Verify the manifest contract points to real final files, including every
  // per-action spritesheet and its same-basename metadata.
  const requireGeneratedFile = (rel, label) => {
    if (typeof rel !== 'string' || !rel) return;
    const inspected = inspectRuntimeAssetPath(rel);
    if (!inspected.ok) {
      fail(`generated asset unsafe: ${label} '${rel}' (${inspected.reason})`);
    } else if (!inspected.exists) {
      fail(`generated asset missing: ${label} '${rel}'`);
    }
  };
  for (const entry of manifestEntries(manifest)) {
    if (!entry || typeof entry !== 'object') continue;
    if (entry.actions && typeof entry.actions === 'object') {
      for (const [action, spec] of Object.entries(entry.actions)) {
        for (const field of ['path', 'metadata']) {
          const rel = spec && spec[field];
          requireGeneratedFile(
            rel,
            `sprite '${entry.key}' action '${action}' ${field}`,
          );
        }
      }
    } else {
      requireGeneratedFile(entry.path, `'${entry.key || '?'}' path`);
    }
    if (entry.layout === 'combined') {
      requireGeneratedFile(
        entry.metadata,
        `legacy sprite '${entry.key}' metadata`,
      );
    }
  }

  const sanityPolicy = readGcaConfig().gates?.asset_sanity;
  if (sanityPolicy?.enabled !== true || noFileAssets) return;

  const formatConfiguredValue = (value) => (
    value === undefined ? '<missing>' : JSON.stringify(value)
  );
  const thresholdIsValid = (key, { positive = false } = {}) => {
    const value = sanityPolicy[key];
    const valid = Number.isInteger(value) && (positive ? value > 0 : value >= 0);
    if (!valid) {
      fail(
        `asset sanity config: gates.asset_sanity.${key} must be a `
        + `${positive ? 'positive' : 'non-negative'} integer when enabled; `
        + `received ${formatConfiguredValue(value)}`,
      );
    }
    return valid;
  };
  const thresholdsValid = [
    thresholdIsValid('min_png_dimension_px', { positive: true }),
    thresholdIsValid('min_unique_files'),
    thresholdIsValid('min_total_bytes'),
  ].every(Boolean);
  if (!thresholdsValid) return;

  const minPngDimension = sanityPolicy.min_png_dimension_px;
  const minUniqueFiles = sanityPolicy.min_unique_files;
  const minTotalBytes = sanityPolicy.min_total_bytes;
  const uniqueHashes = new Set();
  const undersizedPngs = [];
  let totalBytes = 0;

  for (const imagePath of imagePaths) {
    const rel = relative(gameDir, imagePath).split(sep).join('/');
    try {
      totalBytes += statSync(imagePath).size;
    } catch {
      fail(`asset sanity: cannot read file size for '${rel}'`);
      continue;
    }

    const hash = sha256File(imagePath);
    if (hash) {
      uniqueHashes.add(hash);
    } else {
      fail(`asset sanity: cannot hash image file '${rel}'`);
    }

    if (imagePath.endsWith('.png')) {
      const dimensions = pngSize(imagePath);
      if (!dimensions) {
        fail(`asset sanity: PNG '${rel}' is unreadable or structurally invalid`);
      } else if (
        dimensions.w < minPngDimension
        || dimensions.h < minPngDimension
      ) {
        undersizedPngs.push(`${rel} (${dimensions.w}×${dimensions.h})`);
      }
    }
  }

  if (undersizedPngs.length > 0) {
    fail(
      `asset sanity: ${undersizedPngs.length} PNG image(s) are below the `
      + `configured ${minPngDimension}px minimum on either axis: `
      + undersizedPngs.slice(0, 8).join(', ')
      + (undersizedPngs.length > 8 ? `, +${undersizedPngs.length - 8} more` : ''),
    );
  }
  if (uniqueHashes.size < minUniqueFiles) {
    fail(
      `asset sanity: found ${uniqueHashes.size} unique image file content `
      + `hash(es), below the configured minimum of ${minUniqueFiles}`,
    );
  }
  if (totalBytes < minTotalBytes) {
    fail(
      `asset sanity: image files total ${totalBytes} bytes, below the `
      + `configured minimum of ${minTotalBytes} bytes`,
    );
  }
}
function gateAssetIntegrationReport({ requireFinalizedSeal = true } = {}) {
  const reportPath = resolveRead(playtestDir, ASSET_INTEGRATION_REPORT);
  if (!fileExists(reportPath)) {
    fail('asset-integration-report.json missing — run asset-integration-agent before verification');
    return;
  }
  const report = readJSON(reportPath);
  if (!report) {
    fail('asset-integration-report.json is invalid JSON');
    return;
  }
  const manifestPath = join(gameDir, 'assets', 'asset-manifest.json');
  const manifest = readJSON(manifestPath);
  const entries = manifest ? manifestEntries(manifest) : null;
  const blocking = Array.isArray(report.blocking_issues)
    ? report.blocking_issues
    : null;
  if (
    report.schema_version !== 2
    || report.producer !== 'gca-mcp.write_asset_integration_report'
    || !entries
    || !Array.isArray(report.checks)
    || report.checks.length !== entries.length
    || !blocking
    || report.summary?.manifest_entries !== entries.length
    || report.summary?.blocking_issue_count !== blocking?.length
    || report.manifest_sha256 !== sha256File(manifestPath)
  ) {
    fail('asset-integration-report.json is not a valid MCP-owned integration receipt');
    return;
  }
  if (blocking.length > 0) {
    fail(`asset-integration-report.json has ${blocking.length} blocking issue(s)`);
  }
  const state = readJSON(join(playtestDir, 'state.json'));
  if (['pipeline_run_id', 'build_digest'].some(
    (key) => report[key] !== state?.[key]
  )) {
    fail('asset-integration-report.json is not bound to the current pipeline build');
  }
  const currentRun = readJSON(join(playtestDir, 'verify', 'current-run.json'));
  const verifySealRequired = (
    currentRun?.status === 'active'
    || (requireFinalizedSeal && currentRun?.status === 'finalized')
  );
  if (
    verifySealRequired
    && ['pipeline_run_id', 'build_digest', 'verify_run_id'].some(
      (key) => report[key] !== currentRun[key]
    )
  ) {
    fail('asset-integration-report.json is not sealed to the current verify run');
  }

  const srcM = latestInputMtime([
    join(gameDir, 'GAME_DESIGN.md'),
    join(gameDir, 'index.html'),
    join(gameDir, 'src'),
    join(gameDir, 'levels'),
    join(gameDir, 'design'),
    join(gameDir, 'assets'),
    join(playtestDir, 'tests'),
    join(playtestDir, 'inputs'),
    configPath,
  ]);
  const reportM = statSync(reportPath).mtimeMs;
  if (srcM && reportM < srcM) {
    fail('asset-integration-report.json is stale relative to build inputs — re-run asset integration');
  }
}
// ── Gate: bbox coverage on collidable static assets ────────────────────
// Matted props/tiles keep a transparent margin around the art. If implement-agent
// sizes a static body to the FULL frame, the body's edges sit in that empty margin
// → player stands in mid-air on a platform / gets hit from empty space. assets-agent
// is required to merge each `<asset>.bbox.json` (from mcp__gca__content_bbox) into
// asset-manifest.json under a `bbox` field, OR mark the entry `decorative: true`
// when the prop is non-collidable (decorations, particles).
function gateBboxCoverage() {
  const manifestPath = join(gameDir, 'assets', 'asset-manifest.json');
  if (!fileExists(manifestPath)) return; // gateAssetManifest() already failed
  const manifest = readJSON(manifestPath);
  if (!manifest || typeof manifest !== 'object') return;

  const missing = [];
  const collidableCats = new Set(['prop', 'tile', 'tileset', 'platform', 'obstacle']);
  for (const entry of manifestEntries(manifest)) {
    if (!entry || typeof entry !== 'object') continue;
    if (!collidableCats.has(String(entry.category || '').toLowerCase())) continue;
    if (entry.decorative === true || Array.isArray(entry.cells)) continue;
    if (!entry.bbox || typeof entry.bbox.w !== 'number') {
      missing.push(`manifest[${entry.key || '?'}]`);
    }
  }

  // Tile strips own one bbox per cell rather than one bbox on the parent row.
  function scanCells(node, label) {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      node.forEach((value, index) => scanCells(value, `${label}[${index}]`));
      return;
    }
    if (node.decorative === true) return;
    if (Array.isArray(node.cells)) {
      const cellsMissing = node.cells.filter((cell) => (
        !cell
        || typeof cell !== 'object'
        || !cell.bbox
        || typeof cell.bbox.w !== 'number'
      ));
      if (cellsMissing.length) {
        missing.push(`${label}.cells[${cellsMissing.length}/${node.cells.length}]`);
      }
    }
    for (const [key, value] of Object.entries(node)) {
      if (key !== 'cells') scanCells(value, `${label}.${key}`);
    }
  }
  scanCells(manifest, 'manifest');

  if (missing.length) {
    const preview = missing.slice(0, 8).join(', ') + (missing.length > 8 ? `, +${missing.length - 8} more` : '');
    fail(
      `asset-manifest.json: ${missing.length} collidable static asset(s) missing 'bbox' field — ` +
      `assets-agent must merge mcp__gca__content_bbox results into the manifest entry, ` +
      `OR mark each non-collidable entry "decorative": true. Missing: ${preview}`
    );
  }
}

// ── Gate: crop coverage — every grounded design crop must ship or be waived ──
// design-agent grounds per-element crops (design/crops/<screen>/<label>.png +
// crops.json). Each crop must either be PROMOTED to a GDD §2 asset row (its path
// appears in a §2 `Ref Crop` column) / referenced by the asset manifest, OR be
// explicitly WAIVED in its crops.json ("waived": [...]) because a background or
// sprite row already covers its look. A grounded crop that is neither is an
// ORPHAN — it gets generated by nobody and the scene falls back to plain text,
// which is exactly how designed title/game-over typography silently became Arial.
function gateCropCoverage() {
  const cropPolicy = qualityPolicy().cropCoverage;
  const cropsRoot = join(gameDir, 'design', 'crops');
  const gddPath = join(gameDir, 'GAME_DESIGN.md');
  if (!fileExists(gddPath)) return; // gateBasicStructure() handles a missing GDD
  let gddText = '';
  try { gddText = readFileSync(gddPath, 'utf-8'); } catch { return; }

  const manifestJson = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  const cropRefRe = /design\/crops\/[^\s`"'|)>\]}]+\.png/gi;
  const referenced = new Set(gddText.match(cropRefRe) || []);
  const manifestHashClaims = [];
  const collectManifestRefs = (value) => {
    if (!value || typeof value !== 'object') return;
    if (Array.isArray(value)) {
      value.forEach(collectManifestRefs);
      return;
    }
    for (const [field, child] of Object.entries(value)) {
      if (typeof child === 'string') {
        for (const match of child.match(cropRefRe) || []) referenced.add(match);
        if (/^design\/crops\/.+\.png$/i.test(child)) {
          for (const hashField of [`${field}Sha256`, `${field}_sha256`]) {
            if (Object.hasOwn(value, hashField)) {
              manifestHashClaims.push({
                path: child,
                hash: value[hashField],
                field: hashField,
              });
            }
          }
        }
      }
      collectManifestRefs(child);
    }
  };
  collectManifestRefs(manifestJson);
  if (!fileExists(cropsRoot)) {
    if (referenced.size > 0) {
      fail(
        'crop integrity: GDD/manifest references crop paths but design/crops/ '
        + 'and their producer evidence are missing',
      );
    }
    return;
  }

  const hashRe = /^[a-f0-9]{64}$/;
  const norm = (s) => String(s || '').trim().toLowerCase().replace(/\.png$/i, '');
  const cropManifests = listFiles(cropsRoot, (_p, name) => name === 'crops.json');
  const orphans = [];
  const bgMissing = [];
  const cropRows = new Map();
  const coverageRows = [];
  let totalCrops = 0;

  const resolveEvidencePath = (value, label, {
    required = false,
    expectedSha256 = null,
    png = false,
  } = {}) => {
    const issue = canonicalRelativeBrowserPathIssue(value);
    if (issue) {
      fail(`crop integrity: ${label} has unsafe/non-canonical path '${value}': ${issue}`);
      return null;
    }
    const candidate = resolve(gameDir, value);
    if (candidate === gameDir || !candidate.startsWith(`${gameDir}${sep}`)) {
      fail(`crop integrity: ${label} escapes the game workspace`);
      return null;
    }
    if (!fileExists(candidate)) {
      if (required) fail(`crop integrity: ${label} file is missing: ${value}`);
      return null;
    }
    try {
      const stat = lstatSync(candidate);
      const canonicalRoot = realpathSync(gameDir);
      const canonicalFile = realpathSync(candidate);
      if (
        stat.isSymbolicLink()
        || !stat.isFile()
        || (
          canonicalFile !== canonicalRoot
          && !canonicalFile.startsWith(`${canonicalRoot}${sep}`)
        )
      ) {
        fail(`crop integrity: ${label} must be a regular non-symlink workspace file`);
        return null;
      }
    } catch {
      fail(`crop integrity: ${label} cannot be resolved safely`);
      return null;
    }
    if (expectedSha256 !== null && sha256File(candidate) !== expectedSha256) {
      fail(`crop integrity: ${label} sha256 is stale or tampered: ${value}`);
    }
    if (png && !pngSize(candidate)) {
      fail(`crop integrity: ${label} is not a structurally valid PNG: ${value}`);
    }
    return candidate;
  };

  for (const manifestPath of cropManifests) {
    const manifestRel = relative(gameDir, manifestPath).split(sep).join('/');
    const manifestParts = manifestRel.split('/');
    const screen = manifestParts[2] || '';
    if (
      manifestParts.length !== 4
      || manifestParts[0] !== 'design'
      || manifestParts[1] !== 'crops'
      || manifestParts[3] !== 'crops.json'
    ) {
      fail(
        `crop integrity: ${manifestRel} must be exactly `
        + 'design/crops/<scene>/crops.json',
      );
      continue;
    }
    try {
      const stat = lstatSync(manifestPath);
      if (stat.isSymbolicLink() || !stat.isFile()) {
        fail(`crop integrity: ${manifestRel} must be a regular non-symlink file`);
        continue;
      }
    } catch {
      fail(`crop integrity: ${manifestRel} cannot be resolved safely`);
      continue;
    }
    const data = readJSON(manifestPath);
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      fail(`crop integrity: ${manifestRel} is missing or malformed JSON`);
      continue;
    }
    if (!Array.isArray(data.crops) || data.crops.length === 0) {
      fail(`crop integrity: ${manifestRel} must contain a non-empty crops array`);
      continue;
    }
    if (
      !Array.isArray(data.waived)
      || data.waived.some((item) => typeof item !== 'string' || !item)
    ) {
      fail(`crop integrity: ${manifestRel} must contain a string-array waived field`);
      continue;
    }
    const sourceRef = data.image ?? data.source;
    if (
      Object.hasOwn(data, 'image')
      && Object.hasOwn(data, 'source')
      && data.image !== data.source
    ) {
      fail(`crop integrity: ${manifestRel} has conflicting image/source paths`);
    }
    if (
      typeof sourceRef !== 'string'
      || !sourceRef
      || !hashRe.test(String(data.source_sha256 || ''))
    ) {
      fail(`crop integrity: ${manifestRel} is missing image/source_sha256 evidence`);
    } else {
      resolveEvidencePath(sourceRef, `${manifestRel} source`, {
        required: true,
        expectedSha256: data.source_sha256,
      });
    }
    const detectRef = data.detect ?? data.bound_to;
    if (
      Object.hasOwn(data, 'detect')
      && Object.hasOwn(data, 'bound_to')
      && data.detect !== data.bound_to
    ) {
      fail(`crop integrity: ${manifestRel} has conflicting detect/bound_to paths`);
    }
    if (typeof detectRef === 'string' && detectRef) {
      if (!hashRe.test(String(data.detection_sha256 || ''))) {
        fail(`crop integrity: ${manifestRel} is missing detection_sha256 evidence`);
      } else {
        resolveEvidencePath(detectRef, `${manifestRel} detection`, {
          required: true,
          expectedSha256: data.detection_sha256,
        });
      }
    } else if (
      !Object.hasOwn(data, 'detection_sha256')
      || data.detection_sha256 !== null
    ) {
      fail(
        `crop integrity: ${manifestRel} without detect evidence must record `
        + 'detection_sha256:null',
      );
    }
    const waived = new Set(
      data.waived.map(norm)
    );

    // Detect-driven crops (bound_to a detect.json) must cover the BACKGROUND —
    // the full-mockup `background` crop that seeds the §2C background/base asset. Only
    // enforced for the detect-driven flow so legacy VLM-grounded games are unaffected.
    if (cropPolicy === 'all' && detectRef) {
      const hasBg = data.crops.some(
        (c) => c
          && norm(c.label) === 'background'
          && c.source === 'full_mockup'
          && Array.isArray(c.box_px)
          && c.box_px.length === 4
          && Number(c.box_px[0]) === 0
          && Number(c.box_px[1]) === 0,
      );
      if (!hasBg && !waived.has('background')) bgMissing.push(screen);
    }

    for (const crop of data.crops) {
      if (!crop || typeof crop !== 'object' || Array.isArray(crop)) {
        fail(`crop integrity: ${manifestRel} contains a malformed crop row`);
        continue;
      }
      const relPath = crop.path;
      if (
        typeof relPath !== 'string'
        || !relPath
        || !hashRe.test(String(crop.sha256 || ''))
      ) {
        fail(`crop integrity: ${manifestRel} crop rows require path + sha256`);
        continue;
      }
      const expectedPrefix = `design/crops/${screen}/`;
      const issue = canonicalRelativeBrowserPathIssue(relPath);
      const file = relPath.split('/').at(-1) || '';
      if (
        issue
        || !relPath.startsWith(expectedPrefix)
        || relPath.slice(expectedPrefix.length).includes('/')
        || !/\.png$/i.test(file)
      ) {
        fail(
          `crop integrity: ${manifestRel} crop path '${relPath}' must be a `
          + `canonical PNG directly under ${expectedPrefix}`,
        );
        continue;
      }
      if (typeof crop.crop === 'string' && crop.crop !== file) {
        fail(`crop integrity: ${manifestRel} crop filename disagrees with path '${relPath}'`);
        continue;
      }
      if (cropRows.has(relPath)) {
        fail(`crop integrity: duplicate crop evidence row for '${relPath}'`);
        continue;
      }
      resolveEvidencePath(relPath, `${manifestRel} crop`, {
        required: true,
        expectedSha256: crop.sha256,
        png: true,
      });
      cropRows.set(relPath, { crop, hash: crop.sha256, manifestRel });
      if (cropPolicy === 'required' && crop.required !== true) continue;
      totalCrops += 1;
      const stem = norm(file);
      const label = norm(crop.label);
      coverageRows.push({ relPath, screen, file, stem, label, waived });
    }
  }

  for (const ref of referenced) {
    const issue = canonicalRelativeBrowserPathIssue(ref);
    if (
      issue
      || !ref.startsWith('design/crops/')
      || ref.split('/').length !== 4
    ) {
      fail(`crop integrity: referenced crop path '${ref}' is not canonical`);
      continue;
    }
    if (!cropRows.has(ref)) {
      fail(
        `crop integrity: referenced crop '${ref}' has no matching current `
        + 'crops.json row/hash',
      );
    }
  }
  for (const claim of manifestHashClaims) {
    const row = cropRows.get(claim.path);
    if (
      !row
      || !hashRe.test(String(claim.hash || ''))
      || row.hash !== claim.hash
    ) {
      fail(
        `crop integrity: manifest ${claim.field} for '${claim.path}' does not `
        + 'match its current crops.json row',
      );
    }
  }
  for (const row of coverageRows) {
    if (row.waived.has(row.stem) || (row.label && row.waived.has(row.label))) continue;
    if (!referenced.has(row.relPath)) orphans.push(`${row.screen}/${row.file}`);
  }

  if (orphans.length) {
    const preview = orphans.slice(0, 10).join(', ') + (orphans.length > 10 ? `, +${orphans.length - 10} more` : '');
    fail(
      `crop coverage (${qualityProfileName()}): ${orphans.length}/${totalCrops} contracted crop(s) are orphaned — ` +
      `selected by design-agent for shipping but never promoted to a GDD §2 asset row (Ref Crop) nor waived. ` +
      `Fix each: add a §2 row whose Ref Crop is that crop, OR add its label to a "waived": [...] array in its design/crops/<screen>/crops.json (when a background/sprite row already covers it). Orphans: ${preview}`
    );
  }

  if (bgMissing.length) {
    fail(
      `crop coverage: ${bgMissing.length} detect-driven screen(s) are missing the required full-mockup background crop — [${bgMissing.join(', ')}]. ` +
      `Re-run ground_crop({ input, output_dir, detect }); retain that crop as a §2C seed only when clean, or waive it as reference-only when foreground-contaminated.`
    );
  }
}

// ── Gate: aspect drift — lettering must keep its source crop's proportions ──
// Stylized lettering/logos/prompts are generated FROM a design crop via
// edit_image + matting, then sized to their GDD §2E row. If the row's dimensions
// don't match the crop's aspect ratio, the asset is NON-UNIFORMLY resized at
// generation time — the lettering gets squashed flat and turns illegible (e.g. a
// 454×128 ≈3.55:1 "PRESS ENTER TO CONTINUE" crop forced into a 360×80 ≈4.5:1
// banner). This gate compares each decorative-UI asset's actual aspect ratio to
// its source crop's (linked via the §2E Ref Crop column) and blocks past tolerance.
function gateAspectDrift() {
  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  if (!manifest) return; // gateAssetManifest() handles a missing/invalid manifest
  const assets = manifestEntries(manifest);
  if (!assets.length) return;

  const gddPath = join(gameDir, 'GAME_DESIGN.md');
  if (!fileExists(gddPath)) return;
  let gddLines = [];
  try { gddLines = readFileSync(gddPath, 'utf-8').split('\n'); } catch { return; }

  const TOL = 0.15; // ≤15% aspect drift tolerated (matting may trim margins)
  const drifted = [];

  for (const a of assets) {
    if (!a || typeof a !== 'object') continue;
    // Only the squash-prone class: decorative lettering / logos / prompts.
    if (!isUiAssetEntry(a.key, a.category)) continue;
    if (a.decorative !== true) continue;
    const key = a.key;
    const relPath = a.path;
    if (!key || !relPath) continue;
    const inspected = inspectRuntimeAssetPath(relPath);
    if (!inspected.ok) {
      fail(
        `aspect drift: '${key}' has an unsafe runtime PNG path '${relPath}': `
        + inspected.reason,
      );
      continue;
    }
    if (!inspected.exists) continue;
    const assetAbs = inspected.path;

    // The row's Ref Crop sits on the same GDD §2E table line as its key.
    const row = gddLines.find((l) => l.includes(key) && /design\/crops\//.test(l));
    if (!row) continue;
    const m = row.match(/design\/crops\/[^\s`)|]+\.png/i);
    if (!m) continue;
    const cropAbs = join(gameDir, m[0]);
    if (!fileExists(cropAbs)) continue;

    const aDim = pngSize(assetAbs);
    const cDim = pngSize(cropAbs);
    if (!aDim) {
      fail(`aspect drift: existing asset PNG for '${key}' is unreadable or structurally invalid`);
      continue;
    }
    if (!cDim) {
      fail(`aspect drift: existing reference crop PNG for '${key}' is unreadable or structurally invalid`);
      continue;
    }
    const aAspect = aDim.w / aDim.h;
    const cAspect = cDim.w / cDim.h;
    if (!Number.isFinite(aAspect) || !Number.isFinite(cAspect) || cAspect === 0) continue;
    const drift = Math.abs(aAspect - cAspect) / cAspect;
    if (drift > TOL) {
      drifted.push(
        `${key} (asset ${aDim.w}×${aDim.h}≈${aAspect.toFixed(2)}:1 vs crop ${cDim.w}×${cDim.h}≈${cAspect.toFixed(2)}:1, ${(drift * 100).toFixed(0)}% off)`
      );
    }
  }

  if (drifted.length) {
    const preview = drifted.slice(0, 10).join('; ') + (drifted.length > 10 ? `; +${drifted.length - 10} more` : '');
    fail(
      `aspect drift: ${drifted.length} lettering/UI asset(s) were non-uniformly resized away from their source crop (squashed/stretched → illegible). ` +
      `Set each §2E row's dimensions to its crop's aspect ratio (uniform scale of the crop), OR uniformly scale + transparently pad to the target box — NEVER stretch to a fixed W×H (e.g. a flat "…×80" banner). ` +
      `Do NOT "fix" this by running fit_image_to_resolution on the asset — that center-pads + flattens alpha, baking opaque black letterbox bars into it. ` +
      `Drifted: ${preview}`
    );
  }
}

// ── Gate: matting complete — non-scene assets must keep their transparency ──
// Blocks two ways an asset ships with an un-removed background:
//  (A) NO alpha channel → flattened to RGB. Either never matted, or run through
//      fit_image_to_resolution (which bakes opaque letterbox bars + drops alpha).
//  (B) RGBA but ~0% GENUINELY-transparent (alpha≈0 nowhere) → generated on a
//      non-key background (e.g. the game's dark theme color) that matting could
//      not key out, so it stays a near-opaque plate. Best-effort: only on
//      decodable 8-bit RGBA. Applies to EVERY non-scene asset; mark a
//      deliberately edge-to-edge-opaque asset "opaque": true to opt out.
function gateMattingComplete() {
  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  if (!manifest) return; // gateAssetManifest() handles a missing/invalid manifest
  const assets = manifestEntries(manifest);
  if (!assets.length) return;

  // `parallax` is absent on purpose. A parallax layer composites above the sky
  // and needs alpha over its content line, so exempting the category hid the
  // exact defect this gate exists to catch. The bottom-most layer, which really
  // is edge-to-edge, opts out with "opaque": true like any other such asset.
  const SCENE_CATS = new Set(['background', 'map', 'scene', 'backdrop']);
  const noAlpha = [];
  const opaque = [];

  for (const a of assets) {
    if (!a || typeof a !== 'object') continue;
    const key = a.key;
    const relPath = a.path;
    if (!key || !relPath) continue;
    const cat = String(a.category || '').toLowerCase();
    if (SCENE_CATS.has(cat)) continue;             // opaque scene art — exempt
    if (a.terrain_only === true || a.opaque === true) continue; // explicit opt-out
    if (!/\.png$/i.test(relPath)) continue;        // only PNGs carry an alpha channel
    const inspected = inspectRuntimeAssetPath(relPath);
    if (!inspected.ok) {
      fail(
        `matting check: '${key}' has an unsafe runtime PNG path '${relPath}': `
        + inspected.reason,
      );
      continue;
    }
    if (!inspected.exists) continue;                // existence handled by other gates
    const info = pngInfo(inspected.path);
    if (!info) {
      fail(`matting check: existing PNG for '${key}' is unreadable or structurally invalid`);
      continue;
    }

    if (!info.hasAlpha) {
      const kind = info.colorType === 2 ? 'RGB'
        : (info.colorType === 0 ? 'grayscale' : `colortype ${info.colorType}`);
      noAlpha.push(`${key} (${relPath}, ${kind} ${info.w}×${info.h})`);
      continue;
    }
    // Every non-scene asset reaching here should have a genuinely transparent
    // region; ~0% means the background was never keyed out.
    if (typeof info.transparentFraction === 'number' &&
        info.transparentFraction < 0.005) {
      opaque.push(`${key} (${relPath}, RGBA but ${(info.transparentFraction * 100).toFixed(1)}% genuinely transparent)`);
    }
  }

  if (noAlpha.length) {
    const preview = noAlpha.slice(0, 10).join('; ') + (noAlpha.length > 10 ? `; +${noAlpha.length - 10} more` : '');
    fail(
      `matting incomplete: ${noAlpha.length} non-scene asset(s) have NO alpha channel (opaque/flattened) — never matted, OR fit_image_to_resolution flattened them to RGB and baked opaque letterbox bars. ` +
      `Generate on a solid #FF00FF bg → rmbg2_matting → transparent PNG; NEVER run fit_image_to_resolution on assets/**. Offenders: ${preview}`
    );
  }
  if (opaque.length) {
    const preview = opaque.slice(0, 10).join('; ') + (opaque.length > 10 ? `; +${opaque.length - 10} more` : '');
    fail(
      `matting incomplete: ${opaque.length} prop/tile/lettering asset(s) are fully opaque (no transparent pixels) — the generated background was never removed (a dark non-key color is unremovable by chroma-key). ` +
      `Generate on solid #FF00FF/#00F000 then run rmbg2_matting. Offenders: ${preview}`
    );
  }
}

// ── Gate: profile-scoped per-FX spritesheet policy ────────────────────
// VFX (explosions / hit-impacts / slashes / trails / dust / glows / dissolves /
// summon rings / shield breaks) MUST NOT be generated as i2v spritesheets.
// They are recipe calls assembled at runtime from 9 shared atoms. This gate
// catches regressions where assets-agent or implement-agent slips back to the
// old fx-* spritesheet path. Projectile bodies (the bullet/arrow/fireball
// sprite itself) are still allowed; only fx-*-sheet.* / fx-* manifest entries
// are flagged.
//
// Two checks:
//   1. asset-manifest.json: no entries whose key starts with "fx-" / "fx_" /
//      category in {fx, vfx, effect, particle} unless category=particle_atom.
//   2. assets/ tree: no files matching fx-*-sheet.png / fx-*-sheet.json /
//      assets/fx/fx-*.png / assets/sprites/fx-*/play-sheet.png. Old archived
//      copies under assets/.archived_i2v_fx/** are ignored.
function gateNoFxSpritesheets() {
  if (!qualityPolicy().forbidFxSheets) return;
  const issues = [];

  // 1. Manifest scan
  const manifestPath = join(gameDir, 'assets', 'asset-manifest.json');
  if (fileExists(manifestPath)) {
    const manifest = readJSON(manifestPath);
    if (manifest && typeof manifest === 'object') {
      const FX_KEY = /^fx[-_]/i;
      const ATOM_KEY = /^atom_/i;
      const FX_CATS = new Set(['fx', 'vfx', 'effect', 'effects', 'particle', 'particles']);

      // Atoms are runtime-built textures (buildAtoms in particle_recipes.js) —
      // they MUST NOT appear as manifest entries anymore. A leftover atom_* row
      // means a stale scaffold or assets-agent ran the old pipeline. Either way
      // it's a contract violation: the manifest claims a file that scaffold
      // didn't create and that nothing loads.
      function flagEntry(label, key, category) {
        if (typeof key === 'string' && ATOM_KEY.test(key)) {
          issues.push(`${label} (key=${key}, category=${category ?? '?'}) — atoms are runtime-built, remove from manifest`);
          return;
        }
        if (category === 'particle_atom') {
          issues.push(`${label} (key=${key}, category=particle_atom) — particle_atom is obsolete; atoms are runtime-built via buildAtoms()`);
          return;
        }
        if ((typeof key === 'string' && FX_KEY.test(key)) || FX_CATS.has(category)) {
          issues.push(`${label} (key=${key}, category=${category ?? '?'})`);
        }
      }

      for (const entry of manifestEntries(manifest)) {
        if (!entry || typeof entry !== 'object') continue;
        flagEntry(
          `manifest[${entry.key || '?'}]`,
          entry.key,
          String(entry.category || '').toLowerCase(),
        );
      }
    }
  }

  // 2. Filesystem scan — disallow fx-*-sheet.png artefacts AND legacy atom PNGs
  const assetsDir = join(gameDir, 'assets');
  if (existsSync(assetsDir)) {
    const fxFiles = [];
    function walk(d) {
      let entries;
      try { entries = readdirSync(d, { withFileTypes: true }); } catch { return; }
      for (const e of entries) {
        if (e.name.startsWith('.archived_i2v_fx') || e.name.startsWith('.archived_atoms') || e.name.startsWith('.archive')) continue;
        const p = join(d, e.name);
        if (e.isDirectory()) walk(p);
        else if (/^fx[-_].*-sheet\.(png|json)$/i.test(e.name) || /^fx[-_].*-spritesheet\.(png|json)$/i.test(e.name)) {
          fxFiles.push(p.replace(gameDir + '/', ''));
        }
        else if (/^atom_.*\.(png|json)$/i.test(e.name)) {
          fxFiles.push(`${p.replace(gameDir + '/', '')} (legacy atom PNG — atoms are runtime-built, archive to assets/.archived_atoms/)`);
        }
      }
    }
    walk(assetsDir);
    for (const f of fxFiles) issues.push(`fs:${f}`);
  }

  if (issues.length) {
    const preview = issues.slice(0, 8).join(', ') + (issues.length > 8 ? `, +${issues.length - 8} more` : '');
    fail(
      `${issues.length} per-FX / atom artefact(s) found — VFX must go through particle recipes and atoms are runtime-built via buildAtoms() (.claude/skills/game-pipeline/references/implement/fx-guide.md). ` +
      `Remove fx-* manifest entries, atom_* manifest entries, and fx-*-sheet.png / atom_*.png files (archive to assets/.archived_i2v_fx/ or assets/.archived_atoms/ if rollback needed). ` +
      `Found: ${preview}`
    );
  }
}

// ── Gate: subject margin — every generated sprite/prop keeps the subject ──
// inside a canvas with ≥N% empty margin on each edge. Seedance i2v and
// qwen-image-2 both occasionally compose the subject flush against an edge
// (or filling the entire frame). When that happens downstream motion clips,
// the bbox touches the frame, RMBG sometimes mis-mattes the edge-touching
// area, and the asset shows as "the subject fills the whole image" with no usable padding.
//
// This gate reads each `*-sheet.json` / static-asset bbox + frame size and
// flags entries where any edge has < gates.subject_margin.<edge> fraction of
// clear space between the bbox and the frame edge.
function isSheetMetadataName(name) {
  return /(?:-sheet|-spritesheet)\.json$/i.test(name) ||
    /^spritesheet-meta\.json$/i.test(name);
}

function gateSubjectMargin() {
  const cfg = readGcaConfig().gates?.subject_margin ?? {};
  const top    = Number(cfg.top    ?? 0.05);
  const bottom = Number(cfg.bottom ?? 0.05);
  const left   = Number(cfg.left   ?? 0.05);
  const right  = Number(cfg.right  ?? 0.05);
  const enabled = cfg.enabled !== false; // default on
  if (!enabled) return;

  const violations = [];

  // Walk every *-sheet.json under assets/ — these are the sprite/animation
  // metadata files written by assets-agent. Each carries frameWidth,
  // frameHeight, contentBbox.
  const assetsDir = join(gameDir, 'assets');
  if (!existsSync(assetsDir)) return;

  function walk(d) {
    let entries;
    try { entries = readdirSync(d, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (e.name.startsWith('.archive')) continue;
      const p = join(d, e.name);
      if (e.isDirectory()) { walk(p); continue; }
      if (!isSheetMetadataName(e.name)) continue;
      const data = readJSON(p);
      if (!data || typeof data !== 'object') continue;
      const bbox = data.contentBbox;
      const fw = data.frameWidth, fh = data.frameHeight;
      if (!bbox || typeof fw !== 'number' || typeof fh !== 'number') continue;
      if (typeof bbox.x !== 'number' || typeof bbox.y !== 'number' ||
          typeof bbox.w !== 'number' || typeof bbox.h !== 'number') continue;
      const rel = p.replace(gameDir + '/', '');
      // Allow a 1-px slop in the metadata (assets-agent rounds).
      const slop = 1;
      const tMargin = bbox.y / fh;
      const bMargin = (fh - (bbox.y + bbox.h)) / fh;
      const lMargin = bbox.x / fw;
      const rMargin = (fw - (bbox.x + bbox.w)) / fw;
      const bad = [];
      if (bbox.y > -slop && tMargin < top)    bad.push(`top=${(tMargin*100).toFixed(1)}% (<${(top*100).toFixed(0)}%)`);
      if (bbox.y + bbox.h < fh + slop && bMargin < bottom) bad.push(`bottom=${(bMargin*100).toFixed(1)}% (<${(bottom*100).toFixed(0)}%)`);
      if (bbox.x > -slop && lMargin < left)   bad.push(`left=${(lMargin*100).toFixed(1)}% (<${(left*100).toFixed(0)}%)`);
      if (bbox.x + bbox.w < fw + slop && rMargin < right)  bad.push(`right=${(rMargin*100).toFixed(1)}% (<${(right*100).toFixed(0)}%)`);

      // Exception: a sprite WHOSE FEET should anchor to the bottom edge
      // (most platform / side beat_em_up characters) legitimately has bMargin ≈ 0
      // because the foot-anchor block is the generator contract. Only
      // flag bottom when bbox.y + bbox.h is meaningfully INSIDE the
      // frame (i.e. NOT foot-anchored).
      if (data.facing && data.facing !== 'none') {
        // character — drop the bottom check (foot anchor expected)
        const idx = bad.findIndex(s => s.startsWith('bottom'));
        if (idx >= 0) bad.splice(idx, 1);
      }
      if (bad.length) violations.push({ file: rel, bbox, frame: { w: fw, h: fh }, bad });
    }
  }
  walk(assetsDir);

  if (violations.length) {
    const preview = violations.slice(0, 5).map(v =>
      `${v.file}: ${v.bad.join(', ')} (bbox ${v.bbox.w}×${v.bbox.h} in ${v.frame.w}×${v.frame.h})`
    ).join('; ');
    fail(
      `subject margin: ${violations.length} sprite/prop sheet(s) have the subject touching or crossing an edge. ` +
      `Seedance / qwen needs ≥${Math.round(Math.min(top,bottom,left,right)*100)}% empty margin on every edge so downstream motion / matting doesn't clip the subject. ` +
      `Regenerate with the SUBJECT_MARGIN_BLOCK prompt (image_gen.py) — it should already be on by default; if it failed anyway, lower the requested subject size in the prompt. ` +
      `Examples: ${preview}${violations.length > 5 ? `, +${violations.length - 5} more` : ''}`
    );
  }
}

// ── Gate: deterministic entity scale / proportion / collision-fit ──────
// (a) In-engine asset×implement coherence, checked from metadata ONLY (no
// image reads) so it is fully reproducible. Reads the integration intermediate
// `.playtest/inputs/display-params.json` (written by asset-integration-agent)
// where each entity carries contentBbox + scale (+ optional bodyWidth/Height,
// sizeClass).
//   rendered content px = contentBbox.{w,h} * scale
// Catches: invisible / screen-sized sprites, collision body that does not match
// the visible art (hit-from-empty-space / tunneling), bbox outside the frame,
// and — when entities declare a `sizeClass` — wrong relative sizing
// (e.g. a boss rendered no larger than the player).
function gateEntityScale() {
  const dpPath = resolveRead(playtestDir, DISPLAY_PARAMS);
  if (!fileExists(dpPath)) {
    if (hasSpriteEntities()) {
      fail('display-params.json missing for sprite entities — rerun asset integration');
    }
    return; // no sprite entities (board/rhythm/ui games) → N/A
  }
  const dp = readJSON(dpPath);
  if (!dp || typeof dp !== 'object') { fail('display-params.json is invalid JSON'); return; }

  const cfg = readGcaConfig().gates?.scale || {};
  const minPx = Number(cfg.min_entity_px ?? 12);
  const maxPx = Number(cfg.max_entity_px ?? 4096);
  const bodyOverflowTol = Number(cfg.body_overflow_tolerance ?? 0.15);
  const bodyMinRatio = Number(cfg.body_min_ratio ?? 0.30);
  const orderTol = Number(cfg.size_class_tolerance ?? 0.10);

  const SIZE_RANK = { player: 1, ally: 1, npc: 1, enemy: 2, elite: 3, miniboss: 3, boss: 4 };

  // (a)'s on-disk artifact: per-entity computed sizes + per-check verdicts,
  // written under the same verify_middle/ home as (b)'s slot verdicts.
  const audit = {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    source: DISPLAY_PARAMS,
    thresholds: { minPx, maxPx, bodyOverflowTol, bodyMinRatio, orderTol },
    entities: [],
    violations: [],
    passed: true,
  };
  // Route every violation through both the gate's fail() AND the audit record.
  const flag = (key, check, msg) => {
    fail(msg);
    audit.violations.push({ key, check, message: msg });
  };
  const entities = [];

  const source = normalizeDisplayParams(dp) || {};
  const measuredKeys = new Set(
    Object.entries(source)
      .filter(([, entry]) => entry && typeof entry === 'object' && !Array.isArray(entry))
      .map(([key]) => key),
  );
  const missingSpriteKeys = [...expectedSpriteKeys()]
    .filter((key) => !measuredKeys.has(key));
  if (missingSpriteKeys.length > 0) {
    flag(
      missingSpriteKeys.join(', '),
      'entity_coverage',
      `display-params entities omit ${missingSpriteKeys.length} required sprite key(s): ${missingSpriteKeys.join(', ')}`,
    );
  }

  for (const [key, e] of Object.entries(source)) {
    if (!e || typeof e !== 'object') continue;
    const scale = e.scale;
    if (typeof scale !== 'number' || !Number.isFinite(scale) || scale <= 0) {
      flag(key, 'scale_invalid', `[scale] ${key}: invalid scale ${JSON.stringify(scale)} (must be a finite number > 0)`);
      continue;
    }
    // Validate body structure before any bbox early-return: pairing and numeric
    // validity do not depend on contentBbox.
    const hasBodyWidth = e.bodyWidth !== undefined;
    const hasBodyHeight = e.bodyHeight !== undefined;
    const hasOffsetX = e.bodyOffsetX !== undefined;
    const hasOffsetY = e.bodyOffsetY !== undefined;
    let bodyLocal = null;
    let bodyWorld = null;
    if (hasBodyWidth !== hasBodyHeight) {
      flag(key, 'body_partial', `[scale] ${key}: bodyWidth/bodyHeight must be provided together as finite numbers > 0`);
    } else if (hasBodyWidth) {
      const dimensionsValid = Number.isFinite(e.bodyWidth) && e.bodyWidth > 0 &&
        Number.isFinite(e.bodyHeight) && e.bodyHeight > 0;
      if (!dimensionsValid) {
        flag(key, 'body_invalid', `[scale] ${key}: bodyWidth/bodyHeight must be finite numbers > 0`);
      }
      if (!['frame', 'world'].includes(e.bodySpace)) {
        flag(key, 'body_space_invalid', `[scale] ${key}: measured bodies require bodySpace 'frame' or 'world'`);
      }
      if (hasOffsetX !== hasOffsetY) {
        flag(key, 'body_offset_partial', `[scale] ${key}: bodyOffsetX/bodyOffsetY must be provided together`);
      } else if (!hasOffsetX) {
        flag(key, 'body_offset_missing', `[scale] ${key}: measured bodies require explicit bodyOffsetX/bodyOffsetY`);
      } else if (!Number.isFinite(e.bodyOffsetX) || !Number.isFinite(e.bodyOffsetY)) {
        flag(key, 'body_offset_invalid', `[scale] ${key}: bodyOffsetX/bodyOffsetY must be finite numbers`);
      }
      if (dimensionsValid && hasOffsetX && hasOffsetY &&
          Number.isFinite(e.bodyOffsetX) && Number.isFinite(e.bodyOffsetY) &&
          ['frame', 'world'].includes(e.bodySpace)) {
        const toLocal = bodySpaceOf(e) === 'world' ? 1 / scale : 1;
        bodyLocal = {
          x: e.bodyOffsetX * toLocal,
          y: e.bodyOffsetY * toLocal,
          w: e.bodyWidth * toLocal,
          h: e.bodyHeight * toLocal,
        };
        bodyWorld = worldBodyDimensions(e);
      }
    } else if (hasOffsetX || hasOffsetY) {
      flag(key, 'body_offset_without_size', `[scale] ${key}: body offsets require bodyWidth/bodyHeight`);
    }

    const bbox = e.contentBbox;
    if (!bbox || !Number.isFinite(bbox.x) || !Number.isFinite(bbox.y) ||
        !Number.isFinite(bbox.w) || !Number.isFinite(bbox.h) ||
        bbox.w <= 0 || bbox.h <= 0) {
      if (hasBodyWidth || hasBodyHeight) {
        flag(key, 'content_bbox_missing_for_body', `[scale] ${key}: a measured collision body requires a finite positive contentBbox`);
      }
      audit.entities.push({ key, scale, contentBbox: bbox ?? null, note: 'no valid contentBbox — size not asserted' });
      continue;
    }
    const fw = e.frameWidth, fh = e.frameHeight;
    if (typeof fw === 'number' && typeof fh === 'number') {
      if (bbox.x < -1 || bbox.y < -1 || bbox.x + bbox.w > fw + 1 || bbox.y + bbox.h > fh + 1) {
        flag(key, 'bbox_outside_frame', `[scale] ${key}: contentBbox ${JSON.stringify(bbox)} falls outside frame ${fw}x${fh}`);
      }
    }
    const renderedH = bbox.h * scale;
    const renderedW = bbox.w * scale;
    if (renderedH < minPx) {
      flag(key, 'too_small', `[scale] ${key}: rendered content height ${renderedH.toFixed(1)}px < ${minPx}px — entity is effectively invisible at this scale`);
    }
    if (renderedH > maxPx) {
      flag(key, 'too_large', `[scale] ${key}: rendered content height ${renderedH.toFixed(1)}px > ${maxPx}px — entity larger than a sane on-screen/GPU bound`);
    }
    // Collision body rectangle vs the complete VISIBLE-art rectangle.
    if (bodyLocal) {
      if (bodyLocal.w > bbox.w * (1 + bodyOverflowTol)) {
        flag(key, 'body_overflow_w', `[scale] ${key}: collision body width ${bodyLocal.w} exceeds visible content width ${bbox.w} (>${Math.round(bodyOverflowTol * 100)}% over)`);
      }
      if (bodyLocal.h > bbox.h * (1 + bodyOverflowTol)) {
        flag(key, 'body_overflow_h', `[scale] ${key}: collision body height ${bodyLocal.h} exceeds visible content height ${bbox.h} (>${Math.round(bodyOverflowTol * 100)}% over)`);
      }
      if (bodyLocal.w < bbox.w * bodyMinRatio) {
        flag(key, 'body_undersized_w', `[scale] ${key}: collision body width ${bodyLocal.w} < ${Math.round(bodyMinRatio * 100)}% of visible content width ${bbox.w}`);
      }
      if (bodyLocal.h < bbox.h * bodyMinRatio) {
        flag(key, 'body_undersized_h', `[scale] ${key}: collision body height ${bodyLocal.h} < ${Math.round(bodyMinRatio * 100)}% of visible content height ${bbox.h}`);
      }
      const tolX = bbox.w * bodyOverflowTol;
      const tolY = bbox.h * bodyOverflowTol;
      const bodyRight = bodyLocal.x + bodyLocal.w;
      const bodyBottom = bodyLocal.y + bodyLocal.h;
      const bboxRight = bbox.x + bbox.w;
      const bboxBottom = bbox.y + bbox.h;
      const overlapW = Math.max(0, Math.min(bodyRight, bboxRight) - Math.max(bodyLocal.x, bbox.x));
      const overlapH = Math.max(0, Math.min(bodyBottom, bboxBottom) - Math.max(bodyLocal.y, bbox.y));
      if (bodyLocal.x < bbox.x - tolX || bodyRight > bboxRight + tolX ||
          bodyLocal.y < bbox.y - tolY || bodyBottom > bboxBottom + tolY ||
          overlapW <= 0 || overlapH <= 0) {
        flag(key, 'body_offset_misaligned',
          `[scale] ${key}: body rect ${JSON.stringify(bodyLocal)} is not aligned with visible contentBbox ${JSON.stringify(bbox)}`);
      }
    }
    const sizeClass = typeof e.sizeClass === 'string' ? e.sizeClass.toLowerCase() : null;
    audit.entities.push({
      key,
      scale,
      contentBbox: bbox,
      renderedContentWidthPx: Number(renderedW.toFixed(2)),
      renderedContentHeightPx: Number(renderedH.toFixed(2)),
      bodyWidth: e.bodyWidth ?? null,
      bodyHeight: e.bodyHeight ?? null,
      bodyOffsetX: e.bodyOffsetX ?? null,
      bodyOffsetY: e.bodyOffsetY ?? null,
      bodySpace: bodySpaceOf(e),
      worldBodyWidthPx: bodyWorld ? Number(bodyWorld.w.toFixed(2)) : null,
      worldBodyHeightPx: bodyWorld ? Number(bodyWorld.h.toFixed(2)) : null,
      sizeClass,
    });
    entities.push({ key, renderedH, sizeClass });
  }

  // Relative-size ordering — opt-in: only entities that declare a known
  // `sizeClass` participate. design implies player < enemy < boss, etc.
  const classed = entities.filter((e) => e.sizeClass && SIZE_RANK[e.sizeClass]);
  for (const a of classed) {
    for (const b of classed) {
      if (SIZE_RANK[a.sizeClass] < SIZE_RANK[b.sizeClass] &&
          a.renderedH > b.renderedH * (1 + orderTol)) {
        flag(a.key, 'relative_size', `[scale] relative size: '${a.key}' (${a.sizeClass}, ${a.renderedH.toFixed(0)}px) is not smaller than '${b.key}' (${b.sizeClass}, ${b.renderedH.toFixed(0)}px) — design implies ${a.sizeClass} < ${b.sizeClass}`);
      }
    }
  }

  // Land (a)'s audit next to (b)'s slot verdicts (best-effort; never breaks the gate).
  audit.passed = audit.violations.length === 0;
  try {
    mkdirSync(join(playtestDir, SCALE_VALIDATION_DIR), { recursive: true });
    writeFileSync(join(playtestDir, SCALE_REPORT), JSON.stringify(audit, null, 2));
  } catch { /* report write is best-effort */ }
}

// ── Gate: asset slot-fit (b) ───────────────────────────────────────────
// Per-entity VLM verdicts written by assets-agent (validate_asset_slot) under
// verify_middle/slot/<key>.json. Each is { key, verdict|passed, failures:[...] }.
// `passed:false` blocks → the asset deviates from its GDD §2 design slot.
function expectedSpriteKeys() {
  const keys = new Set();
  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  const spriteCats = new Set(['sprite', 'character', 'enemy', 'boss', 'npc', 'projectile']);
  for (const entry of manifestEntries(manifest)) {
    if (entry && entry.key && spriteCats.has(String(entry.category || '').toLowerCase())) {
      keys.add(String(entry.key));
    }
  }

  const dir = join(gameDir, 'assets', 'sprites');
  if (!existsSync(dir)) return keys;
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    if (!e.isDirectory()) continue;
    if (e.name.startsWith('.archive')) continue;
    try {
      if (readdirSync(join(dir, e.name)).some(isSheetMetadataName)) keys.add(e.name);
    } catch { /* unreadable subdir — ignore */ }
  }
  return keys;
}

function hasSpriteEntities() {
  return expectedSpriteKeys().size > 0;
}

function expectedSlotKeys() {
  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  return new Set(
    manifestEntries(manifest)
      .filter(
        (entry) => entry?.sourceStrategy === 'generated-image'
          && typeof entry.key === 'string',
      )
      .map((entry) => entry.key),
  );
}

function gateSlotValidation(
  requirePresence,
  { requireFinalizedSeal = true } = {},
) {
  const dir = resolveRead(playtestDir, SLOT_VALIDATION_DIR);
  const files = existsSync(dir) ? listFiles(dir, (_p, n) => n.endsWith('.json')) : [];
  const expected = expectedSlotKeys();
  const manifestPath = join(gameDir, 'assets', 'asset-manifest.json');
  const gddPath = join(gameDir, 'GAME_DESIGN.md');
  const manifest = readJSON(manifestPath);
  const gddText = existsSync(gddPath) ? readFileSync(gddPath, 'utf8') : '';
  const entriesByKey = new Map(
    manifestEntries(manifest)
      .filter((entry) => entry && typeof entry.key === 'string')
      .map((entry) => [entry.key, entry]),
  );
  const currentRun = readJSON(join(playtestDir, 'verify', 'current-run.json'));
  const verifySealRequired = (
    currentRun?.status === 'active'
    || (requireFinalizedSeal && currentRun?.status === 'finalized')
  );
  if (files.length === 0) {
    if (requirePresence && expected.size > 0) {
      fail('slot-validation missing — run validate_asset_slot for every generated-image manifest slot and write .playtest/verify_middle/slot/<key>.json so asset↔design-slot fit is enforced');
    }
    return;
  }
  const reported = new Set();
  for (const f of files) {
    const r = readJSON(f);
    const name = f.split('/').pop();
    if (!r) { fail(`slot-validation ${name} is invalid JSON`); continue; }
    const key = String(r.key || name.replace(/\.json$/i, ''));
    const entry = entriesByKey.get(key);
    if (
      r.schema_version !== 3
      || r.producer !== 'gca-mcp.validate_asset_slot'
      || name !== `${key}.json`
      || !entry
    ) {
      fail(`slot-validation '${key}' is not a canonical MCP-owned receipt`);
      continue;
    }
    if (reported.has(key)) {
      fail(`slot-validation has duplicate reports for '${key}'`);
      continue;
    }
    reported.add(key);
    const imagePath = typeof r.image === 'string'
      ? (r.image.startsWith('/') ? resolve(r.image) : resolve(gameDir, r.image))
      : null;
    const defaultSpec = entry?.actions && typeof entry.actions === 'object'
      ? entry.actions[entry.defaultAction]
      : entry;
    const expectedImagePath = typeof defaultSpec?.path === 'string'
      ? resolve(gameDir, defaultSpec.path)
      : null;
    if (!imagePath || !expectedImagePath || imagePath !== expectedImagePath) {
      fail(`slot-validation '${key}' is not bound to its exact default runtime artifact`);
    }
    const imageHash = imagePath ? sha256File(imagePath) : null;
    if (!imageHash || r.image_sha256 !== imageHash) {
      fail(`slot-validation '${key}' is stale or unbound to its current image`);
    }
    if (r.slot_contract_sha256 !== slotContractSha256(entry)) {
      fail(`slot-validation '${key}' is stale relative to its manifest slot contract`);
    }
    const expectedAction = entry.actions && typeof entry.actions === 'object'
      ? entry.defaultAction
      : null;
    if (r.action !== expectedAction) {
      fail(`slot-validation '${key}' is not bound to its default manifest action`);
    }
    const description = typeof entry.description === 'string' ? entry.description : null;
    if (
      !description
      || r.slot_description_sha256
        !== createHash('sha256').update(description).digest('hex')
    ) {
      fail(`slot-validation '${key}' is not bound to its canonical manifest description`);
    }
    const gddSlotContext = gddSlotContextSha256(
      gddText,
      key,
      description,
      expectedAction,
    );
    if (!gddSlotContext || r.gdd_slot_context_sha256 !== gddSlotContext) {
      fail(`slot-validation '${key}' is stale relative to GAME_DESIGN.md`);
    }
    if (
      verifySealRequired
      && ['pipeline_run_id', 'build_digest', 'verify_run_id'].some(
        (identityKey) => r[identityKey] !== currentRun[identityKey]
      )
    ) {
      fail(`slot-validation '${key}' is not sealed to the current verify run`);
    }
    const passed = r.passed === true ||
      (typeof r.verdict === 'string' && r.verdict.toUpperCase() === 'PASS');
    if (!passed) {
      const ids = Array.isArray(r.failures)
        ? r.failures.map((x) => x.id || x.name).filter(Boolean).join(', ')
        : '';
      fail(`slot-fit FAIL for '${key}' — asset deviates from its GDD §2 design slot${ids ? ` (${ids})` : ''}`);
    }
  }
  if (requirePresence) {
    const missing = [...expected].filter((key) => !reported.has(key));
    if (missing.length) {
      fail(`slot-validation missing for ${missing.length} generated asset(s): ${missing.join(', ')}`);
    }
  }
}
function hasManifestDrivenRuntimeLoader(srcText, claims) {
  const readsManifest =
    /fetch\s*\(\s*['"`][^'"`]*asset-manifest\.json/i.test(srcText)
    || /\.load\.json\s*\([^;\n]*asset-manifest\.json/i.test(srcText)
    || /from\s+['"`][^'"`]*asset-manifest\.json['"`]/i.test(srcText);
  if (!readsManifest || !/\.(?:assets|entries|items)\b/.test(srcText)) return false;

  const calls = [...srcText.matchAll(
    /\.load\.(image|spritesheet|atlas)\s*\(([\s\S]*?)\)/g,
  )];
  const dynamicCall = (kinds) => calls.some((match) => (
    kinds.has(match[1])
    && /\.(?:textureKey|key)\b/.test(match[2])
    && /\.path\b/.test(match[2])
  ));
  const needsStatic = claims.some((claim) => claim.kind === 'static');
  const needsSprite = claims.some((claim) => claim.kind === 'sprite');
  if (needsStatic && !dynamicCall(new Set(['image', 'atlas']))) return false;
  if (
    needsSprite
    && (!/\.actions\b/.test(srcText)
      || !dynamicCall(new Set(['spritesheet'])))
  ) return false;
  return true;
}

export {
  gateAssetManifest,
  gateAssetRuntimeReferences,
  gateAssetsGenerated,
  gateAssetIntegrationReport,
  gateBboxCoverage,
  gateCropCoverage,
  gateAspectDrift,
  gateMattingComplete,
  gateNoFxSpritesheets,
  gateSubjectMargin,
  gateEntityScale,
  gateSlotValidation,
} ;
