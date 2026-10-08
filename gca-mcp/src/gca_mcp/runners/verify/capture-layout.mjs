#!/usr/bin/env node
/**
 * capture-layout.mjs — Per-scene rendered-layout bounds dump (Axis 5: lays-out-like).
 *
 * The MEASURED side of the layout axis. For each scene named in the layout
 * contract (scaffold's projection of GDD §2.0.2), this:
 *   1. starts that scene FRESH (stops the others) so we capture its initial layout,
 *   2. walks the display list and dumps every NAMED object's bounds
 *      ({ name, layoutKey, cx, cy, x, y, w, h, origin, scrollFactor, visible }),
 *   3. writes <output>/<scene>.rendered.json.
 *
 * gateLayoutAlignment then compares these rendered centers against the contract's
 * canvas-normalized boxes (kind-aware: static / spawn / zone). Coordinates are in
 * Phaser GAME space (design resolution), which is resolution-independent under
 * Scale.FIT, so the comparison holds regardless of the window/CSS size.
 *
 * Discoverability depends on the `obj.name = key` convention implement-agent's
 * addUiImage/applyDisplay set — an unnamed laid-out object is invisible here and
 * shows up as a coverage failure in the gate.
 *
 * Usage:
 *   node capture-layout.mjs --url http://localhost:8080 --game playground/my-game
 *   node capture-layout.mjs --url http://localhost:8080 \
 *        --contract path/to/layout-contract.json --output path/to/verify_middle/layout
 *
 * Output: <output>/<scene>.rendered.json  (one per contract scene)
 */

import { mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'fs';
import { resolve, join } from 'path';
import { parseArgs } from 'node:util';

import { chromium } from './load-playwright.mjs';
import { LAYOUT_CONTRACT, LAYOUT_RENDERED_DIR, resolveRead } from './playtest-layout.mjs';
import { RUNTIME_IDENTITY_OPTIONS, verifyRuntimeIdentity } from './runtime-identity.mjs';

const { values: args } = parseArgs({
  options: {
    url:      { type: 'string' },                 // REQUIRED — no default: a stale server on a guessed port silently captures the wrong build
    game:     { type: 'string' },                 // game dir → derive canonical .playtest paths
    contract: { type: 'string' },                 // explicit layout-contract.json override
    output:   { type: 'string' },                 // explicit output dir override
    width:    { type: 'string', default: '1280' },
    height:   { type: 'string', default: '720' },
    settle:   { type: 'string', default: '700' },  // ms to wait after starting a scene
    headed:   { type: 'boolean', default: false },
    ...RUNTIME_IDENTITY_OPTIONS,
  },
});

if (!args.url) {
  console.error('capture-layout: --url is required (the orchestrator-allocated game URL) — refusing to guess a port and capture a stale server.');
  process.exit(2);
}

const WIDTH = Number(args.width);
const HEIGHT = Number(args.height);
const SETTLE = Number(args.settle);
const RUN_ID = `layout-${Date.now()}-${process.pid}`;

// Resolve contract + output: prefer explicit flags, else derive from --game's
// canonical .playtest layout so artifacts land in the registered home.
let contractPath = args.contract ? resolve(args.contract) : null;
let outputDir = args.output ? resolve(args.output) : null;
if ((!contractPath || !outputDir) && args.game) {
  const playtestDir = join(resolve(args.game), '.playtest');
  if (!contractPath) contractPath = resolveRead(playtestDir, LAYOUT_CONTRACT);
  if (!outputDir) outputDir = join(playtestDir, LAYOUT_RENDERED_DIR);
}
if (!contractPath || !outputDir) {
  console.error('capture-layout: provide --game <dir> (canonical paths) or both --contract and --output.');
  process.exit(2);
}

rmSync(outputDir, { recursive: true, force: true });
mkdirSync(outputDir, { recursive: true });

async function openRuntimePage() {
  const browser = await chromium.launch({ headless: !args.headed, args: ['--disable-infobars'] });
  const context = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
  const page = await context.newPage();
  const jsErrors = [];
  page.on('pageerror', (err) => jsErrors.push(err.message));
  console.log(`capture-layout: opening ${args.url} (${WIDTH}x${HEIGHT})…`);
  await page.goto(args.url, { waitUntil: 'networkidle' });
  const runtimeIdentity = await verifyRuntimeIdentity(page, args);
  return { browser, context, page, jsErrors, runtimeIdentity };
}

async function writeNotApplicable(reason) {
  const opened = await openRuntimePage();
  await opened.page.close();
  await opened.context.close();
  await opened.browser.close();
  writeFileSync(join(outputDir, '_run.json'), JSON.stringify({
    schema_version: 2,
    source: 'capture-layout.mjs',
    run_id: RUN_ID,
    generated_at: new Date().toISOString(),
    url: args.url,
    applicable: false,
    reason,
    scenes: [],
    runtime_identity: opened.runtimeIdentity,
  }, null, 2));
}

if (!existsSync(contractPath)) {
  console.error(`capture-layout: layout contract not found: ${contractPath} (design has no §2.0.2 → nothing to capture).`);
  await writeNotApplicable('layout contract missing');
  process.exit(0); // not an error — text-only games legitimately have no contract
}

let contract;
try {
  contract = JSON.parse(readFileSync(contractPath, 'utf8'));
} catch (e) {
  console.error(`capture-layout: contract is invalid JSON: ${e.message}`);
  process.exit(2);
}

const sceneKeys = Object.keys(contract.scenes || {});
if (sceneKeys.length === 0) {
  await writeNotApplicable('layout contract has no scenes');
  console.log('capture-layout: contract has no scenes — nothing to capture (gate goes N/A).');
  process.exit(0);
}

// ── Page-side: dump every NAMED object's bounds in a scene ─────────────────
// Defined as a stringifiable function passed to page.evaluate.
function dumpSceneBounds(sceneKey) {
  const g = window.__phaserGame;
  if (!g || !g.scene) return { error: 'no __phaserGame' };
  const scene = g.scene.getScene(sceneKey);
  if (!scene) return { scene: sceneKey, error: 'scene not found' };
  const canvas = {
    w: (scene.scale && scene.scale.width) || (g.scale && g.scale.width) || g.config.width,
    h: (scene.scale && scene.scale.height) || (g.scale && g.scale.height) || g.config.height,
  };
  const cam = scene.cameras && scene.cameras.main;
  const elements = [];
  const seen = {};
  // parentName = nearest NAMED ancestor (container) — lets the gate exempt
  // container↔child pairs and group cluster members. parentScroll = the ancestor
  // chain's effective scrollFactor: nested factors multiply, so a HUD container
  // with scrollFactor 0 pins its children even though each child reports 1.
  const walk = (list, parentName, parentScroll) => {
    for (const obj of list) {
      if (!obj) continue;
      const name = obj.name;
      const ownSX = (typeof obj.scrollFactorX === 'number') ? obj.scrollFactorX : null;
      const ownSY = (typeof obj.scrollFactorY === 'number') ? obj.scrollFactorY : null;
      const effSX = (ownSX === null ? 1 : ownSX) * (parentScroll ? parentScroll.x : 1);
      const effSY = (ownSY === null ? 1 : ownSY) * (parentScroll ? parentScroll.y : 1);
      if (name && typeof name === 'string') {
        let b = null;
        try { b = (typeof obj.getBounds === 'function') ? obj.getBounds() : null; } catch { b = null; }
        const x = (typeof obj.x === 'number') ? obj.x : (b ? b.x + b.width / 2 : null);
        const y = (typeof obj.y === 'number') ? obj.y : (b ? b.y + b.height / 2 : null);
        // World-space fallback when getBounds failed: obj.x/y are PARENT-local for
        // container children, so resolve through the world transform — a failed
        // getBounds must never record a container-local coordinate as scene space.
        let wx = x, wy = y;
        if (!b) {
          try {
            if (typeof obj.getWorldTransformMatrix === 'function') {
              const mtx = obj.getWorldTransformMatrix();
              if (mtx && typeof mtx.tx === 'number') { wx = mtx.tx; wy = mtx.ty; }
            }
          } catch { /* keep local x/y */ }
        }
        const cx = b ? (b.x + b.width / 2) : wx;
        const cy = b ? (b.y + b.height / 2) : wy;
        const idx = (seen[name] = (seen[name] || 0));
        seen[name] = idx + 1;
        elements.push({
          name,
          layoutKey: (typeof obj.getData === 'function' && obj.getData('layoutKey')) || name,
          instance: idx,                         // 0,1,2… for repeated keys (formations)
          type: obj.type || obj.constructor?.name || 'GameObject',
          parent: parentName || null,            // nearest named ancestor (container)
          x, y, cx, cy,                          // x/y stay raw (parent-local); cx/cy are world space
          w: b ? b.width : (obj.displayWidth ?? null),
          h: b ? b.height : (obj.displayHeight ?? null),
          originX: (typeof obj.originX === 'number') ? obj.originX : null,
          originY: (typeof obj.originY === 'number') ? obj.originY : null,
          scrollFactorX: ownSX,
          scrollFactorY: ownSY,
          effScrollFactorX: effSX,               // ancestry-multiplied (HUD-layer test)
          effScrollFactorY: effSY,
          depth: (typeof obj.depth === 'number') ? obj.depth : null,
          visible: obj.visible !== false,
        });
      }
      // recurse into Containers (their children carry the laid-out names too)
      if (obj && Array.isArray(obj.list) && obj.list.length) {
        walk(obj.list, (name && typeof name === 'string') ? name : parentName, { x: effSX, y: effSY });
      }
    }
  };
  if (scene.children && Array.isArray(scene.children.list)) walk(scene.children.list, null, null);
  return {
    scene: sceneKey,
    canvas,
    cameraScroll: cam ? { x: cam.scrollX, y: cam.scrollY } : null,
    nNamed: elements.length,
    elements,
  };
}

// ── Launch + capture ───────────────────────────────────────────────────────
const { browser, context, page, jsErrors } = await openRuntimePage();

// Wait for the Phaser game (BootScene runs here → assets load → default scene),
// so when we hot-switch scenes below their textures are already cached.
try {
  await page.waitForFunction(
    () => window.__phaserGame && window.__phaserGame.scene && window.__phaserGame.scene.getScenes(true).length > 0,
    { timeout: 15000 },
  );
} catch {
  console.error('capture-layout: window.__phaserGame never became ready (is it exposed in main.js?).');
}
await page.waitForTimeout(800);

const summary = [];
let captureFailed = false;
for (const sceneKey of sceneKeys) {
  // Start the target scene fresh (stop the others) so we snapshot its initial layout.
  const startRes = await page.evaluate((key) => {
    const g = window.__phaserGame;
    if (!g || !g.scene) return { error: 'no __phaserGame' };
    try {
      for (const s of g.scene.getScenes(true)) {
        const k = s.scene.key;
        if (k !== key) { try { g.scene.stop(k); } catch { /* ignore */ } }
      }
      g.scene.start(key);
      return { ok: true };
    } catch (e) { return { error: String(e && e.message || e) }; }
  }, sceneKey);

  await page.waitForTimeout(SETTLE);
  const sceneRuntimeIdentity = await verifyRuntimeIdentity(page, args);

  let dump;
  if (startRes && startRes.error) {
    dump = { scene: sceneKey, error: `could not start scene: ${startRes.error}`, canvas: { w: WIDTH, h: HEIGHT }, elements: [] };
  } else {
    dump = await page.evaluate(dumpSceneBounds, sceneKey);
  }
  dump.capturedAt = Date.now();
  dump.run_id = RUN_ID;
  dump.url = args.url;
  dump.runtime_identity = sceneRuntimeIdentity;
  if (dump.error) captureFailed = true;

  const outPath = join(outputDir, `${sceneKey}.rendered.json`);
  writeFileSync(outPath, JSON.stringify(dump, null, 2));
  const n = dump.elements ? dump.elements.length : 0;
  summary.push(`  ${sceneKey}: ${dump.error ? `ERROR (${dump.error})` : `${n} named object(s)`} → ${outPath}`);
}

const finalRuntimeIdentity = await verifyRuntimeIdentity(page, args);
await page.close();
await context.close();
await browser.close();

writeFileSync(join(outputDir, '_run.json'), JSON.stringify({
  schema_version: 2,
  source: 'capture-layout.mjs',
  run_id: RUN_ID,
  generated_at: new Date().toISOString(),
  url: args.url,
  scenes: sceneKeys,
  runtime_identity: finalRuntimeIdentity,
}, null, 2));

console.log('capture-layout: done.');
console.log(summary.join('\n'));
if (jsErrors.length) {
  console.log(`\ncapture-layout: ${jsErrors.length} JS error(s) during capture:`);
  jsErrors.slice(0, 10).forEach((e) => console.log(`  ${e}`));
}
if (captureFailed) process.exitCode = 1;
