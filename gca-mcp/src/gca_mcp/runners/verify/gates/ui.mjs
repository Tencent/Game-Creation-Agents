/** UI contract gates — mechanically extracted gate domain. */

import {
  readFileSync,
  existsSync,
  writeFileSync,
  mkdirSync,
} from 'fs';

import { join } from 'path';

import {
  resolveRead,
  DISPLAY_PARAMS,
  LAYOUT_CONTRACT,
  UI_SCALE_VALIDATION_DIR,
  UI_SCALE_REPORT,
  LAYOUT_RENDERED_DIR,
} from '../playtest-layout.mjs';

import {
  readJSON,
  pngSize,
  listFiles,
  isFiniteNumber,
} from '../shared/gate-io.mjs';
import {
  gameDir,
  playtestDir,
  fail,
  fileExists,
} from '../gate-context.mjs';
import {
  readGcaConfig,
  isUiAssetEntry,
  inspectRuntimeAssetPath,
  qualityProfileName,
  qualityPolicy,
  readCanvasSize,
  uiRoleResolution,
  isFunctionalWidgetLabel,
  uiAllowsLenientExemption,
  readSrcText,
  stripJsComments,
  manifestEntries,
} from './support.mjs';

// A §2E UI row's Ref Crop sits on the same GDD line as its runtime key. The crop
// PNG's own pixel size IS the element's measured size in the size-accurate mockup
// (box_px), so it doubles as the proportion ground truth.
function findRefCropAbsForKey(gddLines, key) {
  const row = gddLines.find((l) => l.includes(key) && /design\/crops\//.test(l));
  if (!row) return null;
  const m = row.match(/design\/crops\/[^\s)|]+\.png/i);
  if (!m) return null;
  const abs = join(gameDir, m[0]);
  return fileExists(abs) ? abs : null;
}
function expectedUiLedgerKeys() {
  const expected = new Set();
  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  for (const entry of manifestEntries(manifest)) {
    const key = String(entry?.key || '');
    const category = String(entry?.category || '').toLowerCase();
    if (
      key
      && isUiAssetEntry(key, category)
    ) {
      expected.add(key);
    }
  }
  const layout = readJSON(resolveRead(playtestDir, LAYOUT_CONTRACT));
  for (const scene of Object.values(layout?.scenes || {})) {
    const visit = (element) => {
      if (!element || typeof element !== 'object' || Array.isArray(element)) return;
      const key = String(element.key || element.bind || '');
      const label = [key, element.role, element.kind].filter(Boolean).join(' ');
      const source = String(element.sourceStrategy || '');
      if (
        key
        && (
          source === 'live-text'
          || isFunctionalWidgetLabel(label)
          || /\b(ui|menu|status|overlay|readout|label|title|prompt)\b/i.test(label)
        )
      ) {
        expected.add(key);
      }
      for (const child of (
        Array.isArray(element.children) ? element.children : []
      )) {
        visit(child);
      }
    };
    for (const element of (Array.isArray(scene?.elements) ? scene.elements : [])) {
      visit(element);
    }
  }
  return expected;
}

function readUiScaleContract() {
  const cfg = readGcaConfig().gates?.ui_scale;
  const missing = [];
  const requireNumber = (owner, key, path, {
    minimum = 0,
    exclusiveMinimum = false,
    maximum = null,
    integer = false,
  } = {}) => {
    const value = Number(owner?.[key]);
    const below = exclusiveMinimum ? value <= minimum : value < minimum;
    if (
      !Number.isFinite(value)
      || below
      || (maximum !== null && value > maximum)
      || (integer && !Number.isInteger(value))
    ) {
      missing.push(path);
      return null;
    }
    return value;
  };
  if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) {
    return {
      contract: null,
      errors: ['gates.ui_scale is required and must be an object'],
    };
  }
  const minPx = requireNumber(
    cfg,
    'min_px',
    'gates.ui_scale.min_px',
    { exclusiveMinimum: true },
  );
  const maxTexturePx = requireNumber(
    cfg,
    'max_texture_px',
    'gates.ui_scale.max_texture_px',
    { exclusiveMinimum: true, integer: true },
  );
  const aspectTol = requireNumber(
    cfg,
    'aspect_tolerance',
    'gates.ui_scale.aspect_tolerance',
    { maximum: 1 },
  );
  const overlapPx = requireNumber(
    cfg,
    'overlap_min_px',
    'gates.ui_scale.overlap_min_px',
    { maximum: 4096 },
  );
  if (typeof cfg.require_display_size !== 'boolean') {
    missing.push('gates.ui_scale.require_display_size');
  }
  if (typeof cfg.layout_check !== 'boolean') missing.push('gates.ui_scale.layout_check');

  const fracCfg = cfg.max_canvas_fraction;
  const maxFrac = {};
  for (const role of ['hud', 'panel', 'banner']) {
    maxFrac[role] = requireNumber(
      fracCfg,
      role,
      `gates.ui_scale.max_canvas_fraction.${role}`,
      { exclusiveMinimum: true, maximum: 1 },
    );
  }

  const rolePxCfg = cfg.role_px;
  const rolePx = {};
  for (const role of ['hud', 'panel']) {
    const limits = rolePxCfg?.[role];
    rolePx[role] = {
      maxWidth: requireNumber(
        limits,
        'max_width',
        `gates.ui_scale.role_px.${role}.max_width`,
        { exclusiveMinimum: true, maximum: maxTexturePx, integer: true },
      ),
      maxHeight: requireNumber(
        limits,
        'max_height',
        `gates.ui_scale.role_px.${role}.max_height`,
        { exclusiveMinimum: true, maximum: maxTexturePx, integer: true },
      ),
    };
  }
  if (missing.length > 0) {
    return {
      contract: null,
      errors: [`UI scale config is incomplete or invalid: ${missing.join(', ')}`],
    };
  }
  return {
    contract: {
      minPx,
      maxTexturePx,
      aspectTol,
      overlapPx,
      requireDisplay: cfg.require_display_size,
      layoutCheck: cfg.layout_check,
      maxFrac,
      rolePx,
    },
    errors: [],
  };
}

function writeUiScaleAudit(audit) {
  try {
    mkdirSync(join(playtestDir, UI_SCALE_VALIDATION_DIR), { recursive: true });
    writeFileSync(join(playtestDir, UI_SCALE_REPORT), JSON.stringify(audit, null, 2));
  } catch { /* best-effort */ }
}

function checkUiFitness({
  key, bucket, dw, dh, canvasW, canvasH, maxFrac, rec, flag,
}) {
  if (!(canvasW > 0 && canvasH > 0)) return;
  const fracW = dw / canvasW;
  const fracH = dh / canvasH;
  rec.canvasFractionW = Number(fracW.toFixed(3));
  rec.canvasFractionH = Number(fracH.toFixed(3));
  const cap = maxFrac[bucket];
  if (fracW > cap || fracH > cap) {
    flag(
      key,
      'oversized',
      `[ui-fitness] ${key}: occupies ${(Math.max(fracW, fracH) * 100).toFixed(0)}% of the canvas (> ${(cap * 100).toFixed(0)}% allowed for a '${bucket}' element).`,
    );
  }
}

function checkUiPhysical({
  key,
  bucket,
  dw,
  dh,
  tex,
  nativeUiSize,
  integerUiScale,
  minPx,
  maxTexturePx,
  aspectTol,
  rolePx,
  allowedIntegerScales,
  authoredTargetWidth,
  authoredTargetHeight,
  rec,
  flag,
}) {
  if (dw < minPx || dh < minPx) {
    flag(
      key,
      'too_small',
      `[ui-physical] ${key}: rendered ${dw}×${dh}px is below ${minPx}px — effectively invisible.`,
    );
  }
  if (!tex) return;
  rec.textureWidth = tex.w;
  rec.textureHeight = tex.h;
  if (tex.w > maxTexturePx || tex.h > maxTexturePx) {
    flag(
      key,
      'texture_limit',
      `[ui-physical] ${key}: asset PNG is ${tex.w}×${tex.h}px, above the ${maxTexturePx}px texture safety ceiling.`,
    );
  }
  if (nativeUiSize && bucket !== 'banner') {
    const limits = rolePx[bucket];
    if (tex.w > limits.maxWidth || tex.h > limits.maxHeight) {
      flag(
        key,
        'oversized_asset',
        `[ui-physical] ${key}: asset PNG is ${tex.w}×${tex.h}px, above the loose untouched-source guard ${limits.maxWidth}×${limits.maxHeight}px for '${bucket}' art. Apply the authored §2E uniform target before shipping.`,
      );
    }
  }
  if (
    nativeUiSize
    && Number(authoredTargetWidth) > 0
    && Math.abs(tex.w - Number(authoredTargetWidth)) > 0.5
  ) {
    flag(
      key,
      'authored_target_mismatch',
      `[ui-physical] ${key}: texture width ${tex.w}px does not match manifest targetWidth ${authoredTargetWidth}px.`,
    );
  }
  if (
    nativeUiSize
    && Number(authoredTargetHeight) > 0
    && Math.abs(tex.h - Number(authoredTargetHeight)) > 0.5
  ) {
    flag(
      key,
      'authored_target_mismatch',
      `[ui-physical] ${key}: texture height ${tex.h}px does not match manifest targetHeight ${authoredTargetHeight}px.`,
    );
  }
  if (!(tex.w > 0 && tex.h > 0)) return;
  const dispAspect = dw / dh;
  const texAspect = tex.w / tex.h;
  if (nativeUiSize && (Math.abs(dw - tex.w) > 0.5 || Math.abs(dh - tex.h) > 0.5)) {
    flag(
      key,
      'non_native_size',
      `[ui-physical] ${key}: art_directed renders UI 1:1; displayed ${dw}×${dh}px but texture is ${tex.w}×${tex.h}px.`,
    );
  }
  if (Number.isFinite(dispAspect) && texAspect > 0) {
    const aDrift = Math.abs(dispAspect - texAspect) / texAspect;
    rec.textureAspect = Number(texAspect.toFixed(3));
    if (aDrift > aspectTol) {
      flag(
        key,
        'aspect_drift',
        `[ui-physical] ${key}: displayed aspect ${dispAspect.toFixed(2)}:1 differs ${(aDrift * 100).toFixed(0)}% from the asset PNG's own ${texAspect.toFixed(2)}:1 — resize uniformly; non-uniform distortion is never allowed.`,
      );
    }
    if (integerUiScale) {
      const sx = dw / tex.w;
      const sy = dh / tex.h;
      const nearest = Math.round(sx);
      if (
        nearest < 1
        || Math.abs(sx - nearest) > 0.02
        || Math.abs(sy - nearest) > 0.02
      ) {
        flag(
          key,
          'non_integer_scale',
          `[ui-physical] ${key}: pixel_art requires one integer uniform scale; rendered scale is ${sx.toFixed(3)}×${sy.toFixed(3)}.`,
        );
      } else if (
        !Array.isArray(allowedIntegerScales)
        || !allowedIntegerScales.includes(nearest)
      ) {
        flag(
          key,
          'undeclared_integer_scale',
          `[ui-physical] ${key}: rendered at ${nearest}x, which is not declared in manifest integerScales.`,
        );
      }
    }
  }
}

// Gate: UI proportion / scale — the deterministic "proportion control" enforcement.
// Reads the `ui` block of display-params.json (the on-screen displayWidth/Height
// asset-integration-agent applied) and cross-checks it against the asset PNG's
// real texture size (disk), the §1 canvas, and the element's measured concept crop.
function gateUiProportion() {
  const policy = qualityPolicy();
  const nativeUiSize = policy.nativeUiSize;
  const expectedKeys = expectedUiLedgerKeys();
  const dpPath = resolveRead(playtestDir, DISPLAY_PARAMS);
  if (!fileExists(dpPath)) {
    if (expectedKeys.size > 0) {
      fail(
        `display-params.json is missing although ${expectedKeys.size} UI element(s) `
        + `require measured bounds: ${[...expectedKeys].slice(0, 8).join(', ')}`,
      );
    }
    return;
  }
  const dp = readJSON(dpPath);
  if (!dp || typeof dp !== 'object') {
    if (expectedKeys.size > 0) fail('display-params.json is invalid; UI bounds cannot be verified');
    return;
  }

  const uiRaw = dp.ui;
  if (!uiRaw) {
    if (expectedKeys.size > 0) {
      fail(
        `display-params.ui ledger is missing ${expectedKeys.size} required UI key(s): `
        + [...expectedKeys].slice(0, 8).join(', '),
      );
    }
    return;
  }
  let uiList = [];
  if (Array.isArray(uiRaw)) uiList = uiRaw.filter((e) => e && typeof e === 'object');
  else uiList = Object.entries(uiRaw).map(([key, v]) => ({ key, ...(v && typeof v === 'object' ? v : {}) }));
  const measuredKeyList = uiList
    .map((entry) => String(entry?.key || ''))
    .filter(Boolean);
  const measuredKeys = new Set(measuredKeyList);
  const duplicateKeys = [...new Set(
    measuredKeyList.filter((key, index) => measuredKeyList.indexOf(key) !== index),
  )];
  if (duplicateKeys.length > 0) {
    fail(
      `display-params.ui contains duplicate measured row(s): `
      + duplicateKeys.slice(0, 8).join(', '),
    );
  }
  const missingKeys = [...expectedKeys].filter((key) => !measuredKeys.has(key));
  if (missingKeys.length > 0) {
    fail(
      `display-params.ui omits ${missingKeys.length} required UI key(s): `
      + missingKeys.slice(0, 8).join(', '),
    );
  }
  if (!uiList.length) return;

  const layoutContract = readJSON(resolveRead(playtestDir, LAYOUT_CONTRACT));
  const layoutByKey = {};
  for (const scene of Object.values(layoutContract?.scenes || {})) {
    const visit = (element) => {
      if (!element || typeof element !== 'object' || Array.isArray(element)) return;
      const key = element.key || element.bind;
      if (key) layoutByKey[String(key)] = element;
      for (const child of (
        Array.isArray(element.children) ? element.children : []
      )) {
        visit(child);
      }
    };
    for (const element of (Array.isArray(scene?.elements) ? scene.elements : [])) {
      visit(element);
    }
  }
  const authoredCanvas = (
    Number(layoutContract?.canvas?.designWidth) > 0
    && Number(layoutContract?.canvas?.designHeight) > 0
  )
    ? {
      w: Number(layoutContract.canvas.designWidth),
      h: Number(layoutContract.canvas.designHeight),
    }
    : null;
  const recordedCanvas = (dp.uiCanvas && dp.uiCanvas.w && dp.uiCanvas.h)
    ? { w: Number(dp.uiCanvas.w), h: Number(dp.uiCanvas.h) }
    : (uiList[0] && uiList[0].canvasWidth
      ? { w: Number(uiList[0].canvasWidth), h: Number(uiList[0].canvasHeight) }
      : null);
  const authorityViolations = [];
  if (
    authoredCanvas
    && recordedCanvas
    && (
      recordedCanvas.w !== Number(authoredCanvas.w)
      || recordedCanvas.h !== Number(authoredCanvas.h)
    )
  ) {
    const message = (
      `[ui-authority] display-params uiCanvas ${recordedCanvas.w}×${recordedCanvas.h} `
      + `does not match layout-contract canvas ${authoredCanvas.w}×${authoredCanvas.h}.`
    );
    fail(message);
    authorityViolations.push({
      key: null,
      check: 'canvas_authority_mismatch',
      message,
    });
  }
  const canvas = authoredCanvas || recordedCanvas || readCanvasSize();
  const canvasW = canvas?.w, canvasH = canvas?.h;
  const contractResult = readUiScaleContract();
  if (!contractResult.contract) {
    for (const error of contractResult.errors) fail(error);
    writeUiScaleAudit({
      schema_version: 2,
      generated_at: new Date().toISOString(),
      source: DISPLAY_PARAMS,
      quality_profile: qualityProfileName(),
      native_ui_size: nativeUiSize,
      canvas: canvasW ? { w: canvasW, h: canvasH } : null,
      thresholds: null,
      elements: [],
      violations: contractResult.errors.map((message) => ({
        key: null,
        check: 'invalid_config',
        message,
      })),
      config_errors: contractResult.errors,
      passed: false,
    });
    return;
  }
  const contract = contractResult.contract;
  const {
    minPx,
    maxTexturePx,
    aspectTol,
    requireDisplay,
    maxFrac,
    rolePx,
    layoutCheck,
    overlapPx,
  } = contract;

  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  const byKey = {};
  for (const e of manifestEntries(manifest)) { if (e && e.key) byKey[String(e.key)] = e; }

  const audit = {
    schema_version: 2, generated_at: new Date().toISOString(), source: DISPLAY_PARAMS,
    quality_profile: qualityProfileName(), native_ui_size: nativeUiSize,
    canvas: canvasW ? { w: canvasW, h: canvasH } : null,
    thresholds: {
      minPx, maxTexturePx, aspectTol, maxFrac, rolePx, layoutCheck, overlapPx,
    },
    elements: [], violations: [...authorityViolations], passed: true,
  };
  const flag = (key, check, msg) => { fail(msg); audit.violations.push({ key, check, message: msg }); };

  for (const e of uiList) {
    const key = e.key;
    if (!key) continue;
    const m = byKey[key];
    const layoutEntry = layoutByKey[key];
    const authoritativeRole = layoutEntry?.role || m?.role || null;
    const authoritativeCategory = m?.category || e.category;
    const roleResolution = uiRoleResolution(
      key,
      authoritativeCategory,
      authoritativeRole,
    );
    const manifestDecorative = m?.decorative === true;
    const decorative = manifestDecorative && uiAllowsLenientExemption(
      key,
      authoritativeCategory,
      authoritativeRole,
      true,
      [layoutEntry?.kind, layoutEntry?.category].filter(Boolean).join(' '),
    );
    if (manifestDecorative && !decorative) {
      flag(
        key,
        'functional_decorative_claim',
        `[ui-authority] ${key}: functional UI cannot use a decorative exemption.`,
      );
    }
    if (e.decorative === true && !decorative) {
      flag(
        key,
        'unauthorized_decorative',
        `[ui-authority] ${key}: display ledger cannot grant decorative exemption; declare it in the GDD/manifest.`,
      );
    }
    // Display size = the texture's on-disk size (drawn 1:1). Accept `width`/
    // `height` (the new contract) or legacy `displayWidth`/`displayHeight`.
    const dw = Number(e.width ?? e.displayWidth);
    const dh = Number(e.height ?? e.displayHeight);
    if (
      e.role
      && authoritativeRole
      && String(e.role).trim().toLowerCase()
        !== String(authoritativeRole).trim().toLowerCase()
    ) {
      flag(
        key,
        'role_authority_mismatch',
        `[ui-authority] ${key}: display role '${e.role}' does not match authored role '${authoritativeRole}'.`,
      );
    }
    const explicitRole = authoritativeRole;
    const {
      roleBucket,
      identityBucket,
      unsafeBanner: identityUnsafeBanner,
      bucket: resolvedBucket,
    } = roleResolution;
    const functionalContext = [layoutEntry?.kind, layoutEntry?.category]
      .filter(Boolean)
      .join(' ');
    const unsafeBanner = identityUnsafeBanner || (
      roleBucket === 'banner'
      && isFunctionalWidgetLabel(functionalContext)
    );
    const bucket = unsafeBanner
      ? (identityBucket === 'panel' ? 'panel' : 'hud')
      : resolvedBucket;
    if (unsafeBanner) {
      flag(
        key,
        'role_semantic_conflict',
        `[ui-authority] ${key}: authored role '${explicitRole}' conflicts with `
        + `${identityBucket ? `key/category semantics '${identityBucket}'` : 'functional widget semantics'}.`,
      );
    }
    const rec = {
      key,
      bucket,
      role: explicitRole || null,
      decorative,
      displayWidth: Number.isFinite(dw) ? dw : null,
      displayHeight: Number.isFinite(dh) ? dh : null,
    };

    if (!Number.isFinite(dw) || !Number.isFinite(dh) || dw <= 0 || dh <= 0) {
      if (requireDisplay && !decorative) {
        flag(key, 'no_display_size', `[ui-scale] ${key}: no rendered size recorded in the display-params ui block.`);
      }
      audit.elements.push({ ...rec, note: 'no display size' });
      continue;
    }

    // Capture the on-screen rect for the overlap/overflow pass (additive — only
    // when the ui entry carries a position). Origin is read from EITHER the
    // `origin: [x,y]` array (the display-params contract) OR legacy
    // `originX`/`originY` scalars; only when neither is present does it fall
    // back to center (0.5) — so an authored `origin:[0,1]` panel is no longer
    // silently mis-placed at its center. `depth` (when recorded) is reported with
    // overlap failures, but it is not a blanket exemption: two UI images that
    // collide still collide even if one renders above the other.
    if (Number.isFinite(Number(e.x)) && Number.isFinite(Number(e.y))) {
      const orig = Array.isArray(e.origin) ? e.origin : null;
      const ox = Number.isFinite(Number(e.originX)) ? Number(e.originX)
        : (orig && Number.isFinite(Number(orig[0])) ? Number(orig[0]) : 0.5);
      const oy = Number.isFinite(Number(e.originY)) ? Number(e.originY)
        : (orig && Number.isFinite(Number(orig[1])) ? Number(orig[1]) : 0.5);
      const depth = Number.isFinite(Number(e.depth)) ? Number(e.depth) : null;
      rec.rect = { x: Number(e.x) - ox * dw, y: Number(e.y) - oy * dh, w: dw, h: dh, depth };
    }

    // Display size IS the texture's on-disk size (the asset pipeline scaled the
    // PNG to its in-game size via scale_uniform; the runtime draws it 1:1). So
    // the gate judges ASSET-SIZE SANITY — is the PNG a reasonable size for its
    // role and does it fit the canvas — NOT "did the code copy a GDD number".
    let tex = null;
    if (m?.path) {
      const inspected = inspectRuntimeAssetPath(m.path);
      if (!inspected.ok) {
        flag(
          key,
          'unsafe_texture_path',
          `[ui-physical] ${key}: runtime texture path '${m.path}' is unsafe (${inspected.reason}).`,
        );
      } else if (inspected.exists) {
        tex = pngSize(inspected.path);
        if (!tex) {
          flag(
            key,
            'unreadable_texture',
            `[ui-physical] ${key}: existing runtime texture is not a structurally valid PNG.`,
          );
        }
      }
    }
    checkUiPhysical({
      key,
      bucket,
      dw,
      dh,
      tex,
      nativeUiSize,
      integerUiScale: policy.integerUiScale,
      minPx,
      maxTexturePx,
      aspectTol,
      rolePx,
      allowedIntegerScales: m?.integerScales,
      authoredTargetWidth: m?.targetWidth,
      authoredTargetHeight: m?.targetHeight,
      rec,
      flag,
    });
    checkUiFitness({
      key, bucket, dw, dh, canvasW, canvasH, maxFrac, rec, flag,
    });
    audit.elements.push(rec);
  }

  // (4) Layout pass — placed widgets must not spill off the canvas or pile on
  // top of each other. Additive: runs only over entries that carried x/y (so
  // it is a no-op / N/A for display-params that omit position). Decorative art
  // (logos/backdrops/styled prompts — bucket 'banner' OR an explicit
  // `decorative:true`) is allowed to overlap and bleed, so skip it: a pulsing
  // "TAP TO START" wordmark that sits over a button is intentional, not a
  // collision, and its key need not contain a banner keyword to be exempt.
  if (layoutCheck && canvasW > 0 && canvasH > 0) {
    const placed = audit.elements.filter((r) => r.rect && r.bucket !== 'banner' && !r.decorative);
    for (const r of placed) {
      const { x, y, w, h } = r.rect;
      const over = Math.max(0, -x) + Math.max(0, (x + w) - canvasW); // px off L+R
      const overY = Math.max(0, -y) + Math.max(0, (y + h) - canvasH);
      if (over > minPx || overY > minPx) {
        flag(r.key, 'offscreen', `[ui-scale] ${r.key}: placed rect (${Math.round(x)},${Math.round(y)} ${w}×${h}) spills off the ${canvasW}×${canvasH} canvas. Move it fully on-screen (adjust CONFIG.ui.${r.key}.x/y or shrink the asset).`);
      }
    }
    // Family base — strip a trailing frame/fill/bg segment so a single widget's
    // own frame↔fill pair (which MUST overlap by design) is never flagged as a
    // collision. `ui-health-frame` and `ui-health-fill` share base `ui-health`.
    const familyBase = (k) => String(k).replace(/-(frame|bg|background|fill|track|border|chrome)$/i, '');
    // Two keys count as the SAME widget's frame↔fill pair only when their shared
    // base is still specific — i.e. it kept a business segment beyond the bare
    // `ui`/`hud` prefix. Otherwise `ui-frame` and `ui-fill` (two unrelated
    // widgets) would collapse to `ui` and wrongly exempt each other.
    const GENERIC_BASE_RE = /^(ui|hud)?[-_]?$/i;
    const samePair = (ka, kb) => {
      const base = familyBase(ka);
      return base === familyBase(kb) && !GENERIC_BASE_RE.test(base);
    };
    // Real overlap is measured on BOTH axes in absolute px (origin-aware bounds),
    // NOT as a fraction of the smaller area — two long thin panels colliding
    // edge-to-edge (e.g. High Score ⨯ Level, 21px) are a tiny area fraction but a
    // visible text collision. Rects are full-texture (incl. transparent padding),
    // so the buffer stays wide enough to absorb incidental padding overlap while
    // still catching a real ~21px edge collision; anything ≤ this many px on
    // either axis is treated as incidental edge/corner touch.
    for (let i = 0; i < placed.length; i++) {
      for (let j = i + 1; j < placed.length; j++) {
        const A = placed[i], B = placed[j];
        const a = A.rect, b = B.rect;
        // (a) A widget's own frame↔fill pair is an intentional stack — skip.
        if (samePair(A.key, B.key)) continue;
        const ix = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
        const iy = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
        if (ix <= overlapPx || iy <= overlapPx) continue; // no real 2-axis overlap
        const da = Number.isFinite(a.depth) ? a.depth : 'unset';
        const db = Number.isFinite(b.depth) ? b.depth : 'unset';
        flag(B.key, 'overlap', `[ui-scale] ${A.key} ⨯ ${B.key}: UI widgets overlap by ${Math.round(ix)}×${Math.round(iy)}px (origin-aware display rects; depths ${da}/${db}) — they collide on screen. This is the "layout used the design box but the runtime footprint is the real PNG size" bug. Lay them out side by side (adjust CONFIG.ui.<key>.x/y), shrink one asset, mark non-interactive decorative/banner art appropriately, or split true continuous widgets into a frame+fill pair.`);
      }
    }
  }

  audit.passed = audit.violations.length === 0;
  writeUiScaleAudit(audit);
}

// Gate: per-element UI art parity — functional widgets must SHIP AS ART, not text.
// (a) A functional HUD-widget crop (bar/button/panel/badge/icon HUD/…) may NOT be
//     waived — waiving drops it to a bare add.text/graphics primitive. It must be
//     a §2E art row seeded from its crop.
// (b) Every §2E UI key that HAS a Ref Crop (real art was intended) and whose PNG
//     exists must be rendered via add.image/add.sprite in src/ — never loaded and
//     then replaced by a primitive.
function gateUiArtParity() {
  const cfg = readGcaConfig().gates?.ui_coverage || {};
  if (cfg.enabled === false) return;
  const blockWaived = (
    qualityPolicy().requireWidgetArt
    && cfg.block_waived_widgets !== false
  );
  const requireWired = cfg.require_wired_as_image !== false;

  if (blockWaived) {
    const layout = readJSON(join(playtestDir, 'inputs', 'layout-contract.json'));
    const runtimeWidgets = [];
    for (const [scene, spec] of Object.entries(layout?.scenes || {})) {
      for (const element of (Array.isArray(spec?.elements) ? spec.elements : [])) {
        const source = String(element?.sourceStrategy || '');
        const label = [
          element?.key,
          element?.role,
          element?.kind,
          element?.category,
        ].filter(Boolean).join(' ');
        if (
          ['procedural', 'composition'].includes(source)
          && isFunctionalWidgetLabel(label)
        ) {
          runtimeWidgets.push(`${scene}/${element.key || element.role || '<unnamed>'}`);
        }
      }
    }
    if (runtimeWidgets.length) {
      fail(
        `ui art parity: art_directed requires functional widget frames as file-backed art, `
        + `but ${runtimeWidgets.length} layout element(s) use procedural/composition sources: `
        + runtimeWidgets.slice(0, 8).join(', ')
      );
    }

    const cropsRoot = join(gameDir, 'design', 'crops');
    if (existsSync(cropsRoot)) {
      const norm = (s) => String(s || '').trim().toLowerCase().replace(/\.png$/i, '');
      const cropManifests = listFiles(cropsRoot, (_p, name) => name === 'crops.json');
      const badWaives = [];
      for (const mp of cropManifests) {
        const data = readJSON(mp);
        if (!data || !Array.isArray(data.waived)) continue;
        const parts = mp.split(/[\\/]/); const screen = parts[parts.length - 2] || '';
        for (const w of data.waived) {
          if (isFunctionalWidgetLabel(w)) badWaives.push(`${screen}/${norm(w)}`);
        }
      }
      if (badWaives.length) {
        const preview = badWaives.slice(0, 8).join(', ') + (badWaives.length > 8 ? `, +${badWaives.length - 8} more` : '');
        fail(`ui coverage: ${badWaives.length} functional HUD-widget crop(s) are waived — bars, buttons, panels, badges, counters and icon HUDs are ART (seeded from their crop), not bare add.text/graphics. Promote each to a §2E UI row (key + that crop as Ref Crop) instead of waiving. Only the changing value (the number) stays runtime text. Waived widgets: ${preview}`);
      }
    }
  }

  if (requireWired) {
    const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
    const gddPath = join(gameDir, 'GAME_DESIGN.md');
    if (!fileExists(gddPath)) return;
    let gddLines = [];
    try { gddLines = readFileSync(gddPath, 'utf-8').split('\n'); } catch { return; }
    const srcText = readSrcText();
    const srcLines = srcText.split(/\r?\n/);
    // A key renders as art via a direct add.image/add.sprite OR via the
    // display-utils helpers (addUiImage / addUiCluster), whose call sites carry
    // the quoted key but not the literal `add.image` token. An addUiCluster
    // member array is routinely wrapped across lines, so collect each call's
    // full argument span (balanced parens) and match keys inside it.
    const clusterSpans = [];
    {
      const reCall = /\baddUiCluster\s*\(/g;
      let mCall;
      while ((mCall = reCall.exec(srcText)) !== null) {
        const start = srcText.indexOf('(', mCall.index);
        let depth = 0;
        for (let i = start; i < srcText.length; i++) {
          const ch = srcText[i];
          if (ch === '(') depth += 1;
          else if (ch === ')') { depth -= 1; if (depth === 0) { clusterSpans.push(srcText.slice(start, i + 1)); break; } }
        }
      }
    }
    const rendersKey = (key) =>
      clusterSpans.some((span) => span.includes(`'${key}'`) || span.includes(`"${key}"`)) ||
      srcLines.some((ln) =>
        (ln.includes('add.image') || ln.includes('add.sprite') || /\baddUi(Image|Cluster)\s*\(/.test(ln)) &&
        (ln.includes(`'${key}'`) || ln.includes(`"${key}"`))
      );
    const notRendered = [];
    for (const e of manifestEntries(manifest)) {
      if (!e || typeof e !== 'object') continue;
      const key = String(e.key || '');
      const cat = String(e.category || '').toLowerCase();
      if (!isUiAssetEntry(key, cat)) continue;
      if (!findRefCropAbsForKey(gddLines, key)) continue;     // only keys design grounded a crop for
      if (!e.path) continue;
      const inspected = inspectRuntimeAssetPath(e.path);
      if (!inspected.ok) {
        fail(
          `ui art parity: '${key}' has an unsafe runtime PNG path '${e.path}': `
          + inspected.reason,
        );
        continue;
      }
      if (!inspected.exists) continue;                         // skip not-yet-generated
      if (!pngSize(inspected.path)) {
        fail(`ui art parity: existing runtime PNG for '${key}' is unreadable or structurally invalid`);
        continue;
      }
      if (!rendersKey(key)) notRendered.push(key);
    }
    if (notRendered.length) {
      const preview = notRendered.slice(0, 8).join(', ') + (notRendered.length > 8 ? `, +${notRendered.length - 8} more` : '');
      fail(`ui art parity: ${notRendered.length} §2E UI asset(s) have a concept crop + generated PNG but are never rendered via add.image/add.sprite in src/ — they are likely drawn as crude add.rectangle/graphics/add.text instead of the real art. Render each with this.add.image(x, y, '<key>'). Missing: ${preview}`);
    }
  }
}

// Gate: UI binding contract — overlay-text anchors + interactive slot/action
// parity for composite UI assets (gauge / dashboard / button / card panel).
// Generalizes gateHudBinding to ANY genre and closes two failure modes:
//   (A) a runtime value has no measured position on the asset, so the number
//       floats off the gauge / a label isn't centered on the button — the asset
//       declares no `anchors`; and
//   (B) the asset's slot/button count & function drift from the code — no slot
//       parity / the action isn't wired.
// Reads the manifest binding fields { value_less, anchors[], slots } that design
// + scaffold measured from the mockup, and checks they are honored in src/.
function gateUiBinding() {
  const cfg = readGcaConfig().gates?.ui_binding || {};
  if (cfg.enabled === false) return;
  const requireAnchors = cfg.require_anchors !== false;
  const requireTextBinding = cfg.require_text_binding !== false;
  const requireSlotParity = cfg.require_slot_parity !== false;
  const requireActionWired = cfg.require_action_wired !== false;

  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  const entries = manifestEntries(manifest);
  const layoutContract = readJSON(resolveRead(playtestDir, LAYOUT_CONTRACT));
  const layoutScenes = layoutContract?.scenes && typeof layoutContract.scenes === 'object'
    ? layoutContract.scenes
    : {};
  const seenKeys = new Set(entries.map((entry) => String(entry?.key || '')));
  for (const scene of Object.values(layoutScenes)) {
    for (const element of (Array.isArray(scene?.elements) ? scene.elements : [])) {
      const key = String(element?.key || element?.id || '');
      if (
        key
        && !seenKeys.has(key)
        && (
          Array.isArray(element?.anchors)
          || element?.slots
          || element?.sourceStrategy
        )
      ) {
        entries.push({ category: 'ui', ...element, key });
        seenKeys.add(key);
      }
    }
  }
  if (!entries.length) return;
  const src = stripJsComments(readSrcText());
  if (!src) {
    fail('[ui-binding] src/ is empty; declared UI bindings cannot be rendered');
    return;
  }

  const isUiKey = (key, cat) => isUiAssetEntry(key, cat);
  // Identifier-ish token from a bind/source path: CONFIG.towers[].cost → "cost".
  const ident = (s) => String(s || '').replace(/[)\]]/g, '').split(/[.\[]/).filter(Boolean).pop() || '';
  const baseOf = (s) => { const m = /CONFIG\.([A-Za-z0-9_$]+)/.exec(String(s || '')); return m ? m[1] : String(s || '').replace(/^CONFIG\./, '').split(/[.\[]/)[0].replace(/[^A-Za-z0-9_$]/g, ''); };
  const textNear = (tok) => !!tok && new RegExp(
    'add\\.text[\\s\\S]{0,500}' + tok
    + '|' + tok + '[\\s\\S]{0,200}add\\.text'
  ).test(src);
  const hostsBindingVisual = (key) => {
    const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const literal = `(?:'${escaped}'|"${escaped}"|\`${escaped}\`)`;
    return new RegExp(
      `(?:\\.name\\s*=\\s*${literal}`
      + `|\\.setName\\s*\\(\\s*${literal}`
      + `|\\b(?:addUiImage|addUiCluster|placeUiImage|applyDisplay)\\s*\\([\\s\\S]{0,300}?${literal}`
      + `|\\.add\\.(?:image|sprite|container|nineSlice|renderTexture)\\s*\\([\\s\\S]{0,300}?${literal})`
    ).test(src);
  };
  const wired = (act) => { const a = String(act || '').replace(/[^A-Za-z0-9_$]/g, ''); return a ? new RegExp('\\b' + a + '\\b').test(src) : true; };
  // Top-level key count of a CONFIG.<base> object literal (depth-1 colon proxy).
  const countConfigKeys = (base) => {
    if (!base) return null;
    const cfgPath = join(gameDir, 'src/config.js');
    if (!fileExists(cfgPath)) return null;
    let t; try { t = readFileSync(cfgPath, 'utf-8'); } catch { return null; }
    const m = new RegExp('\\b' + base + '\\s*:\\s*\\{').exec(t);
    if (!m) return null;
    let i = t.indexOf('{', m.index), depth = 0, end = -1; const start = i;
    for (; i < t.length; i++) { const c = t[i]; if (c === '{') depth++; else if (c === '}') { depth--; if (depth === 0) { end = i; break; } } }
    if (end < 0) return null;
    const block = t.slice(start, end + 1);
    let d = 0, n = 0, inStr = false, q = '';
    for (let k = 0; k < block.length; k++) {
      const c = block[k];
      if (inStr) { if (c === '\\') { k++; continue; } if (c === q) inStr = false; continue; }
      if (c === '"' || c === "'" || c === '`') { inStr = true; q = c; continue; }
      if (c === '{' || c === '[' || c === '(') { d++; continue; }
      if (c === '}' || c === ']' || c === ')') { d--; continue; }
      if (d === 1 && c === ':') n++;
    }
    return n > 0 ? n : null;
  };

  for (const e of entries) {
    if (!e || typeof e !== 'object') continue;
    const key = String(e.key || '');
    if (!isUiKey(key, e.category)) continue;
    const anchors = Array.isArray(e.anchors) ? e.anchors : null;
    const slots = e.slots;
    const hasSlots = Array.isArray(slots) ? slots.length > 0 : (slots && typeof slots === 'object');
    const valueLess = e.value_less === true || e.valueLess === true;
    const bindingDeclared = valueLess || (anchors && anchors.length > 0) || hasSlots;
    if (bindingDeclared && !hostsBindingVisual(key)) {
      fail('[ui-binding] ' + key + ': binding visual is never instantiated/named in src/; omitting the host cannot bypass anchor/slot checks.');
    }

    // (1) A value-less binding asset MUST say where its values/slots go.
    if (requireAnchors && valueLess && !anchors && !hasSlots) {
      fail('[ui-binding] ' + key + ': marked value_less but declares no { anchors } or { slots } — its runtime text/values have no measured position, so they cannot be placed on the asset (the "number floats off the gauge" bug). Add an anchor per overlaid value (xPct/yPct/fontPct measured from the mockup crop) and/or a slots spec.');
    }

    // (2) Each declared anchor: valid geometry + its value rendered as runtime text.
    for (const a of (anchors || [])) {
      const id = String((a && (a.id || a.binds || a.bind)) || '');
      const xp = Number(a && a.xPct), yp = Number(a && a.yPct);
      if (!(xp >= 0 && xp <= 1) || !(yp >= 0 && yp <= 1)) {
        fail('[ui-binding] ' + key + ': anchor ' + (id || '?') + ' has no valid xPct/yPct in [0,1] (fraction of the asset box, measured from the mockup) — without it the text position is a guess.');
      }
      if (requireTextBinding) {
        const tok = ident(a && (a.binds || a.bind)) || ident(id);
        if (tok && !textNear(tok)) {
          fail('[ui-binding] ' + key + ': anchor ' + (id || tok) + ' (binds "' + String((a && (a.binds || a.bind)) || tok) + '") has no matching live text binding.');
        }
      }
    }

    // (3) Slot / button parity + action wiring.
    if (requireSlotParity && hasSlots) {
      if (Array.isArray(slots)) {
        for (const s of slots) {
          const act = String((s && (s.action || s.id)) || '');
          if (requireActionWired && act && !wired(act)) {
            fail('[ui-binding] ' + key + ': slot "' + act + '" — its art exists but the action is never referenced in src/. Wire the hitzone (add.zone/setInteractive) to "' + act + '" so the button count AND function match the asset.');
          }
        }
      } else if (typeof slots === 'object') {
        const source = String(slots.source || slots.from || '');
        const base = baseOf(source);
        const declared = Number(slots.count != null ? slots.count : slots.slots);
        if (source && base) {
          const n = countConfigKeys(base);
          if (Number.isFinite(declared) && n != null && declared !== n) {
            fail('[ui-binding] ' + key + ': declared slot count (' + declared + ') != ' + source + ' size (' + n + ') — the panel/toolbar must render exactly one slot per ' + base + ' entry.');
          }
          const dataDriven = new RegExp('Object\\.(keys|values|entries)\\s*\\(\\s*CONFIG\\.' + base + '\\b|CONFIG\\.' + base + '\\b[\\s\\S]{0,40}\\.length|for[\\s\\S]{0,60}CONFIG\\.' + base + '\\b').test(src);
          if (!dataDriven) {
            fail('[ui-binding] ' + key + ': slots are not laid out FROM ' + source + ' (expected Object.keys(' + source + ') / iterate it) — a hardcoded slot count drifts from the roster. Derive the count and layout from ' + source + ' so art, code, and function stay in lockstep.');
          }
          const act = String(slots.action || '');
          if (requireActionWired && act && !wired(act)) {
            fail('[ui-binding] ' + key + ': slot action "' + act + '" is never referenced in src/ — bind each generated slot to a clickable add.zone calling "' + act + '".');
          }
        }
      }
    }
  }
}
// ── Gate: UI layout within clusters (gateUiLayout) ──────────────────────────
// For every `kind: cluster` row that uses the COMPOSITE form (children[]),
// verify that:
//  1. Every declared child (image or text) actually rendered inside the cluster
//  2. No sibling children overlap within the cluster (the gap contract)
//  3. No child overflows the cluster container's bounds (text doesn't escape)
// N/A when layout-contract.json is absent or has no composite clusters.
function gateUiLayout() {
  const cfg = readGcaConfig();
  if (cfg.gates?.ui_layout?.enabled === false) return;

  const contractPath = resolveRead(playtestDir, LAYOUT_CONTRACT);
  if (!fileExists(contractPath)) return;
  const contract = readJSON(contractPath);
  if (!contract) return;
  const scenes = contract.scenes && typeof contract.scenes === 'object' ? contract.scenes : {};
  const sceneKeys = Object.keys(scenes);
  if (sceneKeys.length === 0) return;

  const renderedDir = join(playtestDir, LAYOUT_RENDERED_DIR);
  const overflowTolPx = isFiniteNumber(cfg.gates?.ui_layout?.overflow_tolerance_px) ? cfg.gates.ui_layout.overflow_tolerance_px : 4;
  const overlapTolPx = isFiniteNumber(cfg.gates?.ui_layout?.overlap_tolerance_px) ? cfg.gates.ui_layout.overlap_tolerance_px : 2;
  const issues = [];

  for (const sceneKey of sceneKeys) {
    const elements = Array.isArray(scenes[sceneKey]?.elements) ? scenes[sceneKey].elements : [];
    const clusters = elements.filter(
      (el) => String(el.kind || '').toLowerCase() === 'cluster' && Array.isArray(el.children) && el.children.length > 0
    );
    if (clusters.length === 0) continue;

    const renderedPath = join(renderedDir, `${sceneKey}.rendered.json`);
    if (!fileExists(renderedPath)) continue;
    const rendered = readJSON(renderedPath) || {};
    const renderedEls = Array.isArray(rendered.elements) ? rendered.elements : [];

    const byParent = {};
    for (const o of renderedEls) {
      if (o && o.parent) (byParent[o.parent] ||= []).push(o);
    }

    for (const cluster of clusters) {
      const clusterKey = cluster.key;
      const children = cluster.children;
      const childrenInContainer = byParent[clusterKey] || [];

      // 1. Check every declared child is rendered
      for (const child of children) {
        const childId = child.kind === 'text' ? child.bind : child.key;
        if (!childId) continue;
        const found = childrenInContainer.find(
          (o) => o.name === childId || o.layoutKey === childId
        );
        if (!found) {
          issues.push(`scene '${sceneKey}': cluster '${clusterKey}' — child '${childId}' (${child.kind}) not rendered inside the container. Use addUiCluster composite form to place all children.`);
        }
      }

      // 2. Check no sibling overlap within the cluster
      const validChildren = childrenInContainer.filter(
        (o) => isFiniteNumber(o.cx) && isFiniteNumber(o.cy) && o.w > 0 && o.h > 0 && o.visible !== false
      );
      for (let i = 0; i < validChildren.length; i++) {
        for (let j = i + 1; j < validChildren.length; j++) {
          const a = validChildren[i], b = validChildren[j];
          const ax = a.cx - a.w / 2, ay = a.cy - a.h / 2;
          const bx = b.cx - b.w / 2, by = b.cy - b.h / 2;
          const ix = Math.max(0, Math.min(ax + a.w, bx + b.w) - Math.max(ax, bx));
          const iy = Math.max(0, Math.min(ay + a.h, by + b.h) - Math.max(ay, by));
          if (ix > overlapTolPx && iy > overlapTolPx) {
            issues.push(`scene '${sceneKey}': cluster '${clusterKey}' — children '${a.name}' and '${b.name}' overlap ${Math.round(ix)}×${Math.round(iy)}px inside the container. Increase gap or shrink-to-fit text.`);
          }
        }
      }

      // 3. Check no child overflows the cluster container bounds
      const containerInst = renderedEls.find(
        (o) => (o.name === clusterKey || o.layoutKey === clusterKey) && o.w > 0 && o.h > 0
      );
      if (containerInst && validChildren.length > 0) {
        const cx0 = containerInst.cx - containerInst.w / 2;
        const cy0 = containerInst.cy - containerInst.h / 2;
        const cx1 = cx0 + containerInst.w;
        const cy1 = cy0 + containerInst.h;
        for (const child of validChildren) {
          const chx0 = child.cx - child.w / 2;
          const chy0 = child.cy - child.h / 2;
          const chx1 = chx0 + child.w;
          const chy1 = chy0 + child.h;
          const overX = Math.max(0, cx0 - chx0) + Math.max(0, chx1 - cx1);
          const overY = Math.max(0, cy0 - chy0) + Math.max(0, chy1 - cy1);
          if (overX > overflowTolPx || overY > overflowTolPx) {
            issues.push(`scene '${sceneKey}': cluster '${clusterKey}' — child '${child.name}' overflows the container bounds by ${Math.round(Math.max(overX, overY))}px. Use shrink-to-fit text or reduce maxChars.`);
          }
        }
      }
    }
  }

  if (issues.length) {
    const preview = issues.slice(0, 5).join('\n  ');
    fail(`ui-layout: ${issues.length} cluster layout issue(s):\n  ${preview}${issues.length > 5 ? '\n  …' : ''}`);
  }
}
// Gate: static text must ship as ART, never runtime Arial (gateStaticTextAsAsset).
// Typography rule: any text whose CONTENT is fixed at design time — labels
// (`Integrity:` / `Time Left:`), captions, titles, button text, section headers,
// instructions, rank words — is designed typography, so it ships as an image:
// baked into its parent widget/frame art when it sits on one, else its own
// lettering asset. Only DYNAMIC values (score, %, timer, lives, roster
// names/costs) stay runtime add.text. A fixed string rendered via add.text() is
// the "designed text collapses to Arial + its size decouples from the frame" bug.
function gateStaticTextAsAsset() {
  if (!qualityPolicy().requireWidgetArt) return;
  const cfg = readGcaConfig().gates?.ui_static_text || {};
  if (cfg.enabled === false) return;
  const raw = readSrcText();
  if (!raw) return;
  const minLetters = Number(cfg.min_word_letters ?? 2);

  // Strip comments so commented-out / example code is never flagged.
  const code = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');

  // Capture optional LHS (incl. `this.`) plus the literal text argument.
  const renderers = [
    /(?:(this\.[\w$]+|[\w$]+)\s*=\s*)?(?:this\.)?add\.text\s*\(\s*[^,]+,\s*[^,]+,\s*(['"`])((?:\\.|(?!\2)[\s\S])*?)\2/g,
  ];
  const wordRe = new RegExp('[A-Za-z\\u00C0-\\uFFFF]{' + minLetters + ',}');
  const flagged = new Set();
  for (const re of renderers) {
    let m;
    while ((m = re.exec(code)) !== null) {
      const lhs = m[1] || '';
      const quote = m[2];
      const text = m[3] || '';
      if (quote === '`' && /\$\{/.test(text)) continue;     // template w/ interpolation → dynamic
      if (!wordRe.test(text)) continue;                     // digits/symbols only (e.g. '0', '00:00') → dynamic placeholder
      if (lhs) {                                            // stored + later .setText( → a live readout, allowed
        const v = lhs.replace(/[^\w$.]/g, '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        if (v && new RegExp(v + '\\s*\\.setText\\s*\\(').test(code)) continue;
      }
      flagged.add(text.trim().slice(0, 40));
    }
  }
  if (flagged.size) {
    const list = Array.from(flagged).slice(0, 8).map((t) => '"' + t + '"').join(', ');
    fail('[ui-static-text] static text rendered as runtime text (' + list + (flagged.size > 8 ? ', …' : '') +
      ') — fixed-content text must ship as ART: bake it into its parent widget/frame art, or render its own lettering image (add.image). Only dynamic values (score/%/timer/lives/roster) may be add.text, and those must be stored + updated via .setText. This is the "designed text collapsed to a runtime font and decoupled from the frame size" bug.');
  }
}

// Composite: the deterministic UI contract (proportion + per-element art parity).
// Twin of gateTowerDefenseContract; the VLM element axis (gateElementAlignment)
// is the qualitative complement, folded into first_pass_complete / ready_to_ship.
function gateUiContract() {
  if (/\badd\.bitmapText\s*\(/.test(stripJsComments(readSrcText()))) {
    fail(
      '[ui-bitmap-text] add.bitmapText is unsupported until a complete '
      + 'manifest-backed font atlas contract exists; use declared live-text or image art',
    );
  }
  gateUiProportion();
  gateUiArtParity();
  gateUiBinding();
  gateStaticTextAsAsset();
}

export {
  gateUiProportion,
  gateUiArtParity,
  gateUiBinding,
  gateUiLayout,
  gateStaticTextAsAsset,
  gateUiContract,
} ;
