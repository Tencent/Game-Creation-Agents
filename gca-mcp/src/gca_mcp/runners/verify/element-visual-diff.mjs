#!/usr/bin/env node
/**
 * element-visual-diff.mjs — Comprehensive multimodal-GDD VISUAL verification.
 *
 * Closes the "mmGDD ↔ running screen" gap with a REAL image comparison at TWO
 * granularities (the "comprehensive visual verification (global + per-element)" axis), instead of a single
 * whole-screen VLM glance:
 *
 *   • GLOBAL  (Axis 1, looks-like)  — each running screen vs its concept mockup
 *     design/<screen>.png.
 *   • ELEMENT (Axis 3, per-element) — each grounded design/crops/<screen>/<crop>.png
 *     re-cropped from the SAME box_px on the RUNNING canvas and paired 1:1 with its
 *     concept crop, then judged by the VLM (method=paired_vlm, reference=concept_crop).
 *     Only crops that map to a stable UI element (layout-contract kind:static or
 *     cluster) are compared — entity/prop/zone crops drift at runtime (their
 *     positions come from the level, not the mockup) and are covered elsewhere.
 *
 * The rendered region is clipped from the running canvas via a design→CSS transform
 * (Scale.FIT aware). Reference + rendered frame the identical box_px, so the compare
 * is apples-to-apples. Position correctness is Axis 5's job (gateLayoutAlignment);
 * this axis judges APPEARANCE (real art? same widget? colors/shape/style? value on it?).
 *
 * Outputs (so the EXISTING gates consume the evidence unchanged):
 *   • verify/alignment/visual/alignment.json   (gateVisualAlignment)
 *   • verify/alignment/element/alignment.json  (gateElementAlignment)
 *   • verify_middle/visual/visual-diff-report.json  (gateVisualDiff — REQUIRED sidecar)
 *   • verify_middle/visual/<scene>.screen.png + <scene>/<crop>.render.png (evidence)
 *
 * The script produces evidence + verdicts; the GATES decide pass/fail (aligned bools
 * + freshness), exactly like capture-layout.mjs → gateLayoutAlignment. A visual
 * mismatch or unavailable VLM request exits 0 (the former is a gate failure; the latter
 * is an explicit `unavailable` warning by default). A capture/script fatal exits
 * non-zero after overwriting the sidecar so stale evidence cannot be reused. The
 * visual fix→re-verify LOOP is driven by the orchestrator; pass `--round N`.
 *
 * Usage:
 *   node element-visual-diff.mjs --game playground/my-game --url http://localhost:8080
 *   node element-visual-diff.mjs --game playground/my-game --map "GameScene=gameplay,LevelCompleteScene=gameover"
 */

import { appendFileSync, mkdirSync, writeFileSync, readFileSync, existsSync, readdirSync, rmSync } from 'fs';
import { resolve, join, relative } from 'path';
import { parseArgs } from 'node:util';

import { LAYOUT_CONTRACT, VISUAL_ALIGNMENT, ELEMENT_ALIGNMENT, VISUAL_DIFF_DIR, VISUAL_DIFF_REPORT, VISUAL_DIFF_HISTORY, resolveRead } from './playtest-layout.mjs';
import { callModel, extractJson, detectProvider } from './llm-client.mjs';
import { chromium } from './load-playwright.mjs';
import { RUNTIME_IDENTITY_OPTIONS, verifyRuntimeIdentity } from './runtime-identity.mjs';
import { readGcaConfigFrom } from './shared/gca-config.mjs';

const { values: args } = parseArgs({
  options: {
    url:      { type: 'string', default: 'http://localhost:8080' },
    game:     { type: 'string' },
    config:   { type: 'string' },
    map:      { type: 'string' },                  // "GameScene=gameplay,LevelCompleteScene=gameover"
    round:    { type: 'string', default: '1' },    // current visual fix-loop round (orchestrator-supplied)
    width:    { type: 'string', default: '1280' },
    height:   { type: 'string', default: '720' },
    settle:   { type: 'string', default: '700' },
    headed:   { type: 'boolean', default: false },
    ...RUNTIME_IDENTITY_OPTIONS,
  },
});

const WIDTH = Number(args.width);
const HEIGHT = Number(args.height);
const SETTLE = Number(args.settle);
const ROUND = Number(args.round);

if (!args.game) {
  console.error('element-visual-diff: --game <dir> is required.');
  process.exit(2);
}
const gameDir = resolve(args.game);
const configPath = resolve(
  args.config || process.env.GCA_CONFIG_PATH || join(gameDir, '..', '..', 'gca.config.yaml'),
);
const playtestDir = join(gameDir, '.playtest');
const designDir = join(gameDir, 'design');
const outDir = join(playtestDir, VISUAL_DIFF_DIR);
const SIDECAR = join(playtestDir, VISUAL_DIFF_REPORT);
const HISTORY = join(playtestDir, VISUAL_DIFF_HISTORY);
const RUN_ID = `visual-${Date.now()}-${process.pid}`;
let runtimeIdentity = null;

// ── Minimal gca.config.yaml reader — shared with gate-check (superset loader) ─
function readGcaConfig() { return readGcaConfigFrom(configPath); }

const cfg = readGcaConfig();
const vcfg = (cfg.gates && cfg.gates.visual) || {};
const AXES = vcfg.axes || {};
const DO_GLOBAL = AXES.global !== false;
const DO_ELEMENT = AXES.element !== false;
const CROP_PAD = Number.isFinite(Number(vcfg.crop_padding_px)) ? Number(vcfg.crop_padding_px) : 6;
const MIN_ELEM_PX = Number.isFinite(Number(vcfg.min_element_px)) ? Number(vcfg.min_element_px) : 16;
const MAX_ROUNDS = Number.isFinite(Number(vcfg.max_rounds)) ? Number(vcfg.max_rounds) : 2;
const VLM_MAX_ATTEMPTS = 1;
const UNAVAILABLE_POLICY = 'warn';
const VLM_MAX_TOKENS = Number(((cfg.assets || {}).vlm || {}).image_max_tokens) || 1200;
const VLM_TIMEOUT = Number(((cfg.assets || {}).vlm || {}).image_timeout_seconds) || 90;

function readJSON(p) { try { return JSON.parse(readFileSync(p, 'utf-8')); } catch { return null; } }
function rel(p) { return relative(gameDir, p); }
function clampClip(c) {
  const x = Math.max(0, Math.min(c.x, WIDTH - 1));
  const y = Math.max(0, Math.min(c.y, HEIGHT - 1));
  const width = Math.max(1, Math.min(c.width, WIDTH - x));
  const height = Math.max(1, Math.min(c.height, HEIGHT - y));
  return { x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height) };
}
// crops.json box_px is [x0,y0,x1,y1]; contract box is normalized [x,y,w,h].
function cropBoxToXYWH(b) { return [b[0], b[1], b[2] - b[0], b[3] - b[1]]; }
function iou(a, b) {
  const ax2 = a[0] + a[2], ay2 = a[1] + a[3], bx2 = b[0] + b[2], by2 = b[1] + b[3];
  const ix = Math.max(0, Math.min(ax2, bx2) - Math.max(a[0], b[0]));
  const iy = Math.max(0, Math.min(ay2, by2) - Math.max(a[1], b[1]));
  const inter = ix * iy;
  if (inter <= 0) return 0;
  const uni = a[2] * a[3] + b[2] * b[3] - inter;
  return uni > 0 ? inter / uni : 0;
}

// ── Contract + design intake ───────────────────────────────────────────────
const contractPath = resolveRead(playtestDir, LAYOUT_CONTRACT);
const contract = existsSync(contractPath) ? readJSON(contractPath) : null;
const state = readJSON(join(playtestDir, 'state.json')) || {};
const QUALITY_PROFILE = String(state.quality_profile || '').toLowerCase();
if (!['art_directed', 'responsive', 'pixel_art', 'minimal'].includes(QUALITY_PROFILE)) {
  throw new Error('state.json must contain a valid quality_profile');
}
const ALL_CROPS_CONTRACTED = QUALITY_PROFILE === 'art_directed';
const PROFILE_GUIDANCE = {
  art_directed: 'Fixed labels and functional widgets should use the authored art and normally render at native texture size.',
  responsive: 'Live/localized text and deliberate runtime UI composition are valid; judge fidelity, readability, and uniform scaling rather than raster parity.',
  pixel_art: 'Bitmap text, pixel primitives, and integer uniform scaling are valid when deliberate; reject filtering, fractional scale, or accidental fallback.',
  minimal: 'Deliberate geometric composition and live text are valid; reject only accidental placeholders or a failure of the declared minimal design.',
}[QUALITY_PROFILE] || '';
const W = (contract && contract.canvas && contract.canvas.designWidth) || WIDTH;
const H = (contract && contract.canvas && contract.canvas.designHeight) || HEIGHT;
const contractScenes = (contract && contract.scenes && typeof contract.scenes === 'object') ? contract.scenes : {};

function listDesignScreens() {
  if (!existsSync(designDir)) return [];
  try {
    return readdirSync(designDir)
      .filter((f) => f.toLowerCase().endsWith('.png'))
      .map((f) => f.replace(/\.png$/i, ''));
  } catch { return []; }
}
const screens = listDesignScreens();

function screenCrops(screen) {
  const cj = join(designDir, 'crops', screen, 'crops.json');
  const data = existsSync(cj) ? readJSON(cj) : null;
  if (!data || !Array.isArray(data.crops)) return { crops: [], waived: new Set() };
  const waived = new Set(Array.isArray(data.waived) ? data.waived : []);
  return { crops: data.crops, waived };
}

// Stable UI element boxes (px) from the contract for a scene — the appearance-
// stable set worth an element-level visual diff (static/cluster; skips spawn/zone).
function staticBoxesForScene(sceneKey) {
  const els = (contractScenes[sceneKey] && Array.isArray(contractScenes[sceneKey].elements))
    ? contractScenes[sceneKey].elements : [];
  return els
    .filter((e) => ['static', 'cluster'].includes(String(e.kind || 'static').toLowerCase()) && Array.isArray(e.box))
    .map((e) => ({ key: e.key, role: e.role, box: [e.box[0] * W, e.box[1] * H, e.box[2] * W, e.box[3] * H] }));
}

// ── scene → screen resolution: explicit --map > box-overlap > name munge ────
function normalizeName(s) { return String(s).toLowerCase().replace(/scene$/, '').replace(/[^a-z0-9]/g, ''); }
function resolveSceneScreenMap() {
  const sceneKeys = Object.keys(contractScenes);
  const map = {};

  if (args.map) {
    for (const pair of args.map.split(',')) {
      const [s, scr] = pair.split('=').map((x) => (x || '').trim());
      if (s && scr) map[s] = scr;
    }
  }

  for (const sceneKey of sceneKeys) {
    if (map[sceneKey]) continue;
    // (a) explicit contract mapping. Responsive/pixel profiles may intentionally
    // share one mockup across several scenes.
    const sceneContract = contractScenes[sceneKey] || {};
    const explicit = sceneContract.reference || sceneContract.mockup || sceneContract.design_ref;
    if (typeof explicit === 'string') {
      const stem = explicit.split(/[\\/]/).pop().replace(/\.png$/i, '');
      const explicitHit = screens.find((scr) => normalizeName(scr) === normalizeName(stem));
      if (explicitHit) { map[sceneKey] = explicitHit; continue; }
    }
    // (b) exact name match (high confidence: TitleScene→title, LevelSelectScene→
    // level-select). Tried FIRST — a scene whose name IS a screen name should not
    // be overridden by an incidental box overlap with a busier screen.
    const munged = normalizeName(sceneKey);
    const nameHit = screens.find((scr) => normalizeName(scr) === munged);
    if (nameHit) { map[sceneKey] = nameHit; continue; }
    // (c) box-overlap: for non-obvious names (GameScene→gameplay,
    // LevelCompleteScene→gameover) score each screen by how well its crop boxes
    // cover this scene's static element boxes (both in the shared W×H space).
    const statics = staticBoxesForScene(sceneKey).map((b) => b.box);
    let best = null, bestScore = 0;
    for (const screen of screens) {
      const { crops, waived } = screenCrops(screen);
      const cboxes = crops
        .filter((c) => !waived.has(c.label) && (ALL_CROPS_CONTRACTED || c.required === true))
        .map((c) => cropBoxToXYWH(c.box_px));
      if (!cboxes.length || !statics.length) continue;
      let score = 0;
      for (const sb of statics) { let m = 0; for (const cb of cboxes) m = Math.max(m, iou(sb, cb)); score += m; }
      score /= statics.length;
      if (score > bestScore) { bestScore = score; best = screen; }
    }
    if (best && bestScore >= 0.12) map[sceneKey] = best;
    // else: leave unmapped (skipped in capture) — pass --map "<Scene>=<screen>".
  }
  return map;
}

// ── Page-side: design→CSS transform (Scale.FIT aware) ──────────────────────
function canvasTransformFn() {
  const g = window.__phaserGame;
  const canvas = g && g.canvas;
  if (!canvas) return null;
  const r = canvas.getBoundingClientRect();
  const dw = (g.scale && g.scale.width) || (g.config && g.config.width) || 1280;
  const dh = (g.scale && g.scale.height) || (g.config && g.config.height) || 720;
  const s = Math.min(r.width / dw, r.height / dh) || 1;
  return {
    offX: r.left + (r.width - dw * s) / 2,
    offY: r.top + (r.height - dh * s) / 2,
    s, dw, dh,
  };
}

// ── VLM prompts ─────────────────────────────────────────────────────────────
const ELEMENT_PROMPT = (label, role) => `You are comparing ONE UI element between its intended DESIGN and what the game ACTUALLY renders.
QUALITY PROFILE: ${QUALITY_PROFILE}. ${PROFILE_GUIDANCE}
IMAGE 1 = the DESIGN concept art for the element "${label}"${role ? ` (role: ${role})` : ''}.
IMAGE 2 = the SAME on-screen region of the actual RUNNING game.
Judge whether IMAGE 2 faithfully realizes IMAGE 1 as the same element. Weigh:
  - deliberate, profile-authorized rendering vs an accidental scaffold placeholder;
  - same KIND of widget (a button reads as that button; a gauge/bar/panel/badge as that widget);
  - shape, colors, and style consistent with the design;
  - roughly the same footprint/proportion within the region (not squashed, not a sliver, not overflowing);
  - any label/value sits ON the element where the design places it (not floating off, clipped, or overlapping the frame).
IGNORE: exact pixel values, minor anti-aliasing, the dynamic text CONTENT (a different score/number/name is fine), and anything in the background outside the element.
Respond with ONLY compact JSON, no prose:
{"match": true|false, "severity": "low"|"medium"|"high", "reason": "<one short sentence>"}
Be strict about accidental fallback, but do not reject profile-authorized live text, composition, or uniform/integer scaling merely because it is not raster-identical.`;

const GLOBAL_PROMPT = (screen) => `You are comparing a whole game SCREEN against its design mockup.
QUALITY PROFILE: ${QUALITY_PROFILE}. ${PROFILE_GUIDANCE}
IMAGE 1 = the DESIGN mockup for the "${screen}" screen (the intended look).
IMAGE 2 = the actual RUNNING game screen.
Judge whether IMAGE 2 realizes the mockup's COMPOSITION, element set, HUD placement, and palette. Look for: missing elements the mockup shows, extra/placeholder elements, wrong layout regions, or a clearly different color/mood.
IGNORE exact pixel values and dynamic content; judge layout/style fidelity.
Respond with ONLY compact JSON, no prose:
{"match": true|false, "severity": "low"|"medium"|"high", "issues": ["<short>", ...], "reason": "<one short sentence>"}`;

// ── Capture phase (Playwright) ──────────────────────────────────────────────
async function capture(sceneScreenMap) {
  const browser = await chromium.launch({ headless: !args.headed, args: ['--disable-infobars'] });
  const context = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
  const page = await context.newPage();
  const jsErrors = [];
  page.on('pageerror', (err) => jsErrors.push(err.message));

  console.log(`element-visual-diff: opening ${args.url} (${WIDTH}x${HEIGHT})…`);
  await page.goto(args.url, { waitUntil: 'networkidle' });
  runtimeIdentity = await verifyRuntimeIdentity(page, args);
  try {
    await page.waitForFunction(
      () => window.__phaserGame && window.__phaserGame.scene && window.__phaserGame.scene.getScenes(true).length > 0,
      { timeout: 15000 },
    );
  } catch {
    console.error('element-visual-diff: window.__phaserGame never became ready (is it exposed in main.js?).');
  }
  await page.waitForTimeout(800);

  const captured = []; // { scene, screen, screenPng, elements:[{label, role, ref, renderPng}] }
  for (const [sceneKey, screen] of Object.entries(sceneScreenMap)) {
    const startRes = await page.evaluate((key) => {
      const g = window.__phaserGame;
      if (!g || !g.scene) return { error: 'no __phaserGame' };
      try {
        for (const s of g.scene.getScenes(true)) { const k = s.scene.key; if (k !== key) { try { g.scene.stop(k); } catch {} } }
        g.scene.start(key);
        return { ok: true };
      } catch (e) { return { error: String((e && e.message) || e) }; }
    }, sceneKey);
    await page.waitForTimeout(SETTLE);
    if (startRes && startRes.error) throw new Error(`${sceneKey}: could not start (${startRes.error})`);

    const tf = await page.evaluate(canvasTransformFn);
    if (!tf) throw new Error(`${sceneKey}: no canvas transform`);

    const sceneOut = join(outDir, sceneKey);
    mkdirSync(sceneOut, { recursive: true });

    // GLOBAL: full canvas screenshot
    let screenPng = null;
    if (DO_GLOBAL && screens.includes(screen)) {
      screenPng = join(outDir, `${sceneKey}.screen.png`);
      const gclip = clampClip({ x: tf.offX, y: tf.offY, width: tf.dw * tf.s, height: tf.dh * tf.s });
      try { await page.screenshot({ path: screenPng, clip: gclip }); }
      catch (e) { throw new Error(`${sceneKey}: screen shot failed (${e.message})`); }
    }

    // ELEMENT: clip each non-waived crop that maps to a stable UI element.
    const elements = [];
    if (DO_ELEMENT) {
      const { crops, waived } = screenCrops(screen);
      const statics = staticBoxesForScene(sceneKey);
      for (const c of crops) {
        if (!c || !c.label || waived.has(c.label) || !Array.isArray(c.box_px)) continue;
        if (!ALL_CROPS_CONTRACTED && c.required !== true) continue;
        // The full-mockup `background` crop is the whole screen → covered by the global axis; skip here.
        if (String(c.label).toLowerCase() === 'background' || String(c.role || '').toLowerCase() === 'background') continue;
        const cbox = cropBoxToXYWH(c.box_px);
        if (cbox[2] < MIN_ELEM_PX || cbox[3] < MIN_ELEM_PX) continue;
        // Compare only stable UI crops (static/cluster). Entity/prop/zone crops
        // drift at runtime and are covered by layout/entity checks.
        // Detect-driven crops carry key/kind → bind directly; legacy crops fall back to a box-overlap join.
        let key = c.key || null;
        let role = c.role || null;
        const carriedKind = c.kind ? String(c.kind).toLowerCase() : null;
        if (carriedKind && !['static', 'cluster'].includes(carriedKind)) continue;
        if (!key || !carriedKind) {
          let bestKey = null, bestIoU = 0;
          for (const sb of statics) { const v = iou(sb.box, cbox); if (v > bestIoU) { bestIoU = v; bestKey = sb; } }
          if (!bestKey || bestIoU < 0.08) {
            throw new Error(`${sceneKey}/${c.label}: stable crop could not be bound to a layout element`);
          }
          key = key || bestKey.key;
          role = role || bestKey.role;
        }
        const refPath = c.path && existsSync(c.path) ? c.path : join(designDir, 'crops', screen, c.crop || `${c.label}.png`);
        if (!existsSync(refPath)) throw new Error(`${sceneKey}/${c.label}: concept crop missing at ${refPath}`);
        const clip = clampClip({
          x: tf.offX + (cbox[0] - CROP_PAD) * tf.s,
          y: tf.offY + (cbox[1] - CROP_PAD) * tf.s,
          width: (cbox[2] + 2 * CROP_PAD) * tf.s,
          height: (cbox[3] + 2 * CROP_PAD) * tf.s,
        });
        const renderPng = join(sceneOut, `${c.label}.render.png`);
        try { await page.screenshot({ path: renderPng, clip }); }
        catch (e) { throw new Error(`${sceneKey}/${c.label}: crop shot failed (${e.message})`); }
        elements.push({ label: c.label, key: key || c.label, role, ref: refPath, renderPng });
      }
    }
    captured.push({ scene: sceneKey, screen, screenPng, elements, expectedElements: elements.length });
    console.log(`  ${sceneKey} → ${screen}: ${screenPng ? 'screen + ' : ''}${elements.length} element crop(s)`);
  }

  runtimeIdentity = await verifyRuntimeIdentity(page, args);
  await page.close(); await context.close(); await browser.close();
  return { captured, jsErrors };
}

async function probeRuntimeIdentity() {
  const browser = await chromium.launch({ headless: !args.headed, args: ['--disable-infobars'] });
  try {
    const context = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await context.newPage();
    await page.goto(args.url, { waitUntil: 'networkidle' });
    runtimeIdentity = await verifyRuntimeIdentity(page, args);
    await context.close();
  } finally {
    await browser.close();
  }
}

// ── Compare phase (paired VLM) ──────────────────────────────────────────────
let vlmCircuitError = null;
let vlmCircuitAttempts = 0;

async function vlmVerdict(prompt, refPath, renderPath, label) {
  if (vlmCircuitError) {
    return { match: null, status: 'unavailable', error: vlmCircuitError };
  }
  const res = await callModel({
    prompt,
    images: [{ path: refPath }, { path: renderPath }],
    maxTokens: VLM_MAX_TOKENS,
    temperature: 0,
    timeout: VLM_TIMEOUT,
    retries: 0,
    label: `visual-diff-${label}`.slice(0, 40),
  });
  if (res.error) {
    const operational = ['api', 'network', 'response'].includes(res.errorKind);
    if (!operational) {
      throw new Error(`VLM ${res.errorKind || 'local'} error: ${res.error}`);
    }
    vlmCircuitError = res.error;
    vlmCircuitAttempts = 1;
    return { match: null, status: 'unavailable', error: res.error, provider: res.provider, attempts: 1 };
  }
  const verdict = extractJson(res.text);
  if (verdict && typeof verdict.match === 'boolean') {
    return {
      match: verdict.match,
      status: verdict.match ? 'passed' : 'failed',
      severity: verdict.severity || (verdict.match === false ? 'medium' : 'low'),
      reason: verdict.reason || '',
      issues: Array.isArray(verdict.issues) ? verdict.issues : [],
      provider: res.provider,
      attempts: 1,
    };
  }
  vlmCircuitError = 'VLM response did not contain a boolean `match` verdict';
  vlmCircuitAttempts = 1;
  return {
    match: null,
    status: 'unavailable',
    error: vlmCircuitError,
    raw: (res.text || '').slice(0, 200),
    provider: res.provider,
    attempts: 1,
  };
}

async function main() {
  if (!Number.isInteger(ROUND) || ROUND < 1 || ROUND > MAX_ROUNDS) {
    throw new Error(`--round must be an integer from 1 to ${MAX_ROUNDS}, got ${args.round}`);
  }
  const previousHistory = existsSync(HISTORY)
    ? readFileSync(HISTORY, 'utf-8')
    : '';
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  if (previousHistory) writeFileSync(HISTORY, previousHistory);

  // Applicability: no concept mockups → visual axis N/A (matches gate semantics).
  if (!screens.length) {
    await probeRuntimeIdentity();
    writeApplicableFalse('no concept mockups (design/<screen>.png) — visual verification N/A');
    console.log('element-visual-diff: no concept mockups — wrote applicable:false and exited.');
    return;
  }
  if (!DO_GLOBAL && !DO_ELEMENT) {
    await probeRuntimeIdentity();
    writeApplicableFalse('all visual axes are disabled');
    console.log('element-visual-diff: all visual axes disabled — wrote applicable:false and exited.');
    return;
  }
  // No VLM provider is an operational outage, not proof of alignment. Record an
  // explicit unavailable verdict; the repository's warning-only outage policy decides
  // whether the gate blocks.
  if (!detectProvider({})) {
    await probeRuntimeIdentity();
    writeUnavailable('no Doubao VLM transport (set ARK_API_KEY) — visual comparison unavailable');
    console.warn(`element-visual-diff: no VLM provider — wrote status:unavailable (${UNAVAILABLE_POLICY}) and exited.`);
    return;
  }

  const sceneScreenMap = resolveSceneScreenMap();
  if (!Object.keys(sceneScreenMap).length) {
    throw new Error('could not map any scene to a design screen (pass --map "Scene=screen,…")');
  }

  const { captured, jsErrors } = await capture(sceneScreenMap);

  const globalChecked = [], globalIssues = [];
  const elementChecked = [], elementIssues = [];
  const sidecarScenes = [];
  let globalFailed = false, elementFailed = false;
  let globalUnavailable = false, elementUnavailable = false;

  for (const cap of captured) {
    const sidecar = { scene: cap.scene, screen: cap.screen, global: null, elements: [] };

    if (cap.screenPng) {
      const designPng = join(designDir, `${cap.screen}.png`);
      const v = await vlmVerdict(GLOBAL_PROMPT(cap.screen), designPng, cap.screenPng, cap.scene);
      const entry = { name: cap.scene, ref: rel(designPng), running: rel(cap.screenPng), match: v.match, status: v.status, notes: v.reason || v.error };
      globalChecked.push(entry);
      sidecar.global = { match: v.match, status: v.status, severity: v.severity, reason: v.reason, error: v.error, provider: v.provider, attempts: v.attempts };
      if (v.match === false) {
        globalFailed = true;
        globalIssues.push({ severity: v.severity || 'medium', description: `screen '${cap.scene}' deviates from mockup ${cap.screen}.png: ${v.reason}`, root_cause: 'whole-screen composition/palette drift vs design/<screen>.png' });
        for (const is of (v.issues || [])) globalIssues.push({ severity: 'medium', description: `${cap.scene}: ${is}`, root_cause: 'global visual mismatch' });
      } else if (v.match !== true) {
        globalUnavailable = true;
        globalIssues.push({ severity: 'warning', description: `screen '${cap.scene}' visual comparison unavailable: ${v.error}`, root_cause: 'pinned Doubao request failed' });
      }
    }

    for (const el of cap.elements) {
      const v = await vlmVerdict(ELEMENT_PROMPT(el.label, el.role), el.ref, el.renderPng, `${cap.scene}-${el.label}`);
      const entry = { name: `${cap.scene}/${el.label}`, key: el.key, ref: rel(el.ref), running: rel(el.renderPng), match: v.match, status: v.status, notes: v.reason || v.error };
      elementChecked.push(entry);
      sidecar.elements.push({ label: el.label, key: el.key, role: el.role, match: v.match, status: v.status, severity: v.severity, reason: v.reason, error: v.error, provider: v.provider, attempts: v.attempts, ref: rel(el.ref), running: rel(el.renderPng) });
      if (v.match === false) {
        elementFailed = true;
        elementIssues.push({ severity: v.severity || 'medium', description: `element '${el.key || el.label}' (${cap.scene}) does not match its concept crop: ${v.reason}`, root_cause: 'per-element appearance mismatch (placeholder / wrong widget / off-anchor) vs design/crops' });
      } else if (v.match !== true) {
        elementUnavailable = true;
        elementIssues.push({ severity: 'warning', description: `element '${el.key || el.label}' (${cap.scene}) visual comparison unavailable: ${v.error}`, root_cause: 'pinned Doubao request failed' });
      }
    }
    sidecarScenes.push(sidecar);
  }

  const expectedSceneCount = Object.keys(sceneScreenMap).length;
  const expectedElementCount = captured.reduce((n, cap) => n + cap.expectedElements, 0);
  if (captured.length !== expectedSceneCount) {
    throw new Error(`captured ${captured.length}/${expectedSceneCount} mapped scene(s)`);
  }
  if (DO_GLOBAL && globalChecked.length !== expectedSceneCount) {
    throw new Error(`global visual coverage ${globalChecked.length}/${expectedSceneCount}`);
  }
  if (DO_ELEMENT && elementChecked.length !== expectedElementCount) {
    throw new Error(`element visual coverage ${elementChecked.length}/${expectedElementCount}`);
  }
  if ((DO_GLOBAL ? globalChecked.length : 0) + (DO_ELEMENT ? elementChecked.length : 0) === 0) {
    throw new Error('visual verification produced zero comparisons for enabled axes');
  }

  const nowIso = new Date().toISOString();
  const globalStatus = globalFailed ? 'failed' : (globalUnavailable ? 'unavailable' : 'passed');
  const elementStatus = elementFailed ? 'failed' : (elementUnavailable ? 'unavailable' : 'passed');
  const globalAligned = globalStatus === 'failed' ? false : (globalStatus === 'unavailable' ? null : true);
  const elementAligned = elementStatus === 'failed' ? false : (elementStatus === 'unavailable' ? null : true);
  // Existing-schema alignment files (consumed by gateVisualAlignment / gateElementAlignment).
  if (DO_GLOBAL) {
    writeJSON(VISUAL_ALIGNMENT, { axis: 'visual', applicable: true, status: globalStatus, aligned: globalAligned, source: 'element-visual-diff.mjs', run_id: RUN_ID, round: ROUND, generated_at: nowIso, checked: globalChecked, issues: globalIssues });
  }
  if (DO_ELEMENT) {
    writeJSON(ELEMENT_ALIGNMENT, { axis: 'element', applicable: true, status: elementStatus, aligned: elementAligned, source: 'element-visual-diff.mjs', run_id: RUN_ID, round: ROUND, generated_at: nowIso, checked: elementChecked, issues: elementIssues });
  }

  // Required deterministic sidecar (gateVisualDiff): proves the loop actually ran.
  const failed = (DO_GLOBAL && globalFailed) || (DO_ELEMENT && elementFailed);
  const unavailable = !failed && ((DO_GLOBAL && globalUnavailable) || (DO_ELEMENT && elementUnavailable));
  const status = failed ? 'failed' : (unavailable ? 'unavailable' : 'passed');
  const aligned = status === 'failed' ? false : (status === 'unavailable' ? null : true);
  const unavailableCount = sidecarScenes.reduce((n, s) => (
    n + (s.global?.status === 'unavailable' ? 1 : 0)
      + s.elements.filter((e) => e.status === 'unavailable').length
  ), 0);
  writeSidecar({
    schema_version: 2,
    source: 'element-visual-diff.mjs',
    run_id: RUN_ID,
    generated_at: nowIso,
    round: ROUND,
    max_rounds: MAX_ROUNDS,
    max_attempts: VLM_MAX_ATTEMPTS,
    unavailable_policy: UNAVAILABLE_POLICY,
    url: args.url,
    method: vcfg.method || 'paired_vlm',
    reference: vcfg.reference || 'concept_crop',
    axes: { global: DO_GLOBAL, element: DO_ELEMENT },
    applicable: true,
    status,
    aligned,
    global_aligned: DO_GLOBAL ? globalAligned : null,
    element_aligned: DO_ELEMENT ? elementAligned : null,
    unavailable_count: unavailableCount,
    unavailable_reason: status === 'unavailable' ? vlmCircuitError : undefined,
    attempts_used: status === 'unavailable' ? vlmCircuitAttempts : undefined,
    coverage: {
      expected_scenes: expectedSceneCount,
      captured_scenes: captured.length,
      compared_global: globalChecked.length,
      expected_elements: expectedElementCount,
      compared_elements: elementChecked.length,
    },
    scene_screen_map: sceneScreenMap,
    scenes: sidecarScenes,
    js_errors: jsErrors.slice(0, 10),
  });

  console.log(`element-visual-diff: round ${ROUND}/${MAX_ROUNDS} — status=${status}, aligned=${aligned} (global=${globalStatus}, element=${elementStatus}).`);
  console.log(`  global: ${globalChecked.length} screen(s), element: ${elementChecked.length} widget(s).`);
  if (status === 'unavailable') console.warn(`  VLM circuit opened after ${vlmCircuitAttempts} attempt(s); remaining comparisons were not retried. policy=${UNAVAILABLE_POLICY}.`);
  console.log(`  sidecar → ${SIDECAR}`);
}

// ── Writers ──────────────────────────────────────────────────────────────
function writeJSON(rel_, obj) {
  const p = join(playtestDir, rel_);
  mkdirSync(p.replace(/\/[^/]+$/, ''), { recursive: true });
  writeFileSync(p, JSON.stringify({
    quality_profile: QUALITY_PROFILE,
    runtime_identity: runtimeIdentity,
    ...obj,
  }, null, 2));
}
function writeSidecar(obj) {
  mkdirSync(outDir, { recursive: true });
  obj = {
    quality_profile: QUALITY_PROFILE,
    runtime_identity: runtimeIdentity,
    ...obj,
  };
  writeFileSync(SIDECAR, JSON.stringify(obj, null, 2));
  if (
    Number(obj.schema_version) >= 2
    && obj.applicable === true
    && (obj.status === 'passed' || obj.status === 'failed')
  ) {
    appendFileSync(HISTORY, `${JSON.stringify({
      schema_version: 1,
      source: obj.source,
      quality_profile: QUALITY_PROFILE,
      run_id: obj.run_id,
      generated_at: obj.generated_at,
      status: obj.status,
      aligned: obj.aligned,
      coverage: obj.coverage,
    })}\n`);
  }
}
function writeApplicableFalse(reason) {
  const nowIso = new Date().toISOString();
  if (DO_GLOBAL) writeJSON(VISUAL_ALIGNMENT, { axis: 'visual', applicable: false, status: 'not_applicable', aligned: null, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso, reason, checked: [], issues: [] });
  if (DO_ELEMENT) writeJSON(ELEMENT_ALIGNMENT, { axis: 'element', applicable: false, status: 'not_applicable', aligned: null, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso, reason, checked: [], issues: [] });
  writeSidecar({ schema_version: 2, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso, round: ROUND, max_rounds: MAX_ROUNDS, applicable: false, status: 'not_applicable', aligned: null, reason });
}
function writeUnavailable(reason) {
  const nowIso = new Date().toISOString();
  const issue = { severity: 'warning', description: reason, root_cause: 'pinned Doubao request unavailable' };
  if (DO_GLOBAL) writeJSON(VISUAL_ALIGNMENT, { axis: 'visual', applicable: true, status: 'unavailable', aligned: null, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso, reason, checked: [], issues: [issue] });
  if (DO_ELEMENT) writeJSON(ELEMENT_ALIGNMENT, { axis: 'element', applicable: true, status: 'unavailable', aligned: null, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso, reason, checked: [], issues: [issue] });
  writeSidecar({
    schema_version: 2, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso,
    round: ROUND, max_rounds: MAX_ROUNDS, max_attempts: VLM_MAX_ATTEMPTS,
    unavailable_policy: UNAVAILABLE_POLICY, method: vcfg.method || 'paired_vlm',
    reference: vcfg.reference || 'concept_crop', axes: { global: DO_GLOBAL, element: DO_ELEMENT },
    applicable: true, status: 'unavailable', aligned: null, global_aligned: null,
    element_aligned: null, unavailable_count: 1, scene_screen_map: {}, scenes: [], reason,
  });
}
function writeFatal(error) {
  const nowIso = new Date().toISOString();
  const reason = `visual diff fatal: ${error && error.message ? error.message : String(error)}`;
  const issue = { severity: 'high', description: reason, root_cause: 'capture/script failure before a trustworthy visual verdict' };
  if (DO_GLOBAL) writeJSON(VISUAL_ALIGNMENT, { axis: 'visual', applicable: true, status: 'error', aligned: null, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso, reason, checked: [], issues: [issue] });
  if (DO_ELEMENT) writeJSON(ELEMENT_ALIGNMENT, { axis: 'element', applicable: true, status: 'error', aligned: null, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso, reason, checked: [], issues: [issue] });
  writeSidecar({
    schema_version: 2, source: 'element-visual-diff.mjs', run_id: RUN_ID, generated_at: nowIso,
    round: ROUND, max_rounds: MAX_ROUNDS, max_attempts: VLM_MAX_ATTEMPTS,
    unavailable_policy: UNAVAILABLE_POLICY, method: vcfg.method || 'paired_vlm',
    reference: vcfg.reference || 'concept_crop', axes: { global: DO_GLOBAL, element: DO_ELEMENT },
    applicable: true, status: 'error', aligned: null, global_aligned: null,
    element_aligned: null, scene_screen_map: {}, scenes: [], reason,
  });
}

main().then(() => process.exit(0)).catch((e) => {
  console.error(`element-visual-diff: fatal ${e && e.stack || e}`);
  try { writeFatal(e); } catch (writeError) { console.error(`element-visual-diff: failed to write fatal sidecar: ${writeError.message}`); }
  process.exit(1);
});
