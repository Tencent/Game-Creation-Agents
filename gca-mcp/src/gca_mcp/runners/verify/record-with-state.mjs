#!/usr/bin/env node
/**
 * record-with-state.mjs — Record game video + capture state timeline simultaneously.
 *
 * Wraps Playwright recording with periodic state extraction (every 1s) using
 * its own inline compact extractor (COMPACT_STATE_FN below — kept conceptually
 * in sync with state-extractor.mjs). Produces:
 *   - A .webm video recording
 *   - A state-timeline.json with timestamped game state snapshots
 *
 * The state-timeline.json is later consumed by the legacy-named
 * gemini-video-analyze.mjs runner to provide Doubao with ground-truth game state
 * alongside video, eliminating false positives.
 *
 * Usage:
 *   node record-with-state.mjs --url http://localhost:8080 --duration 60
 *   node record-with-state.mjs --url http://localhost:8080 --script interact.mjs --output .playtest/verify/recording/
 *   node record-with-state.mjs --url http://localhost:8080 --interval 500  # capture every 500ms
 *
 * Output:
 *   <output-dir>/recording.webm
 *   <output-dir>/state-timeline.json
 *   <output-dir>/performance-summary.json
 */

import { mkdirSync, writeFileSync, existsSync, renameSync, unlinkSync, readFileSync } from 'fs';
import { resolve, join } from 'path';
import { parseArgs } from 'node:util';

import { chromium } from './load-playwright.mjs';
import { RUNTIME_IDENTITY_OPTIONS, verifyRuntimeIdentity } from './runtime-identity.mjs';

const { values: args } = parseArgs({
  options: {
    url:       { type: 'string' },
    output:    { type: 'string', default: '.playtest/verify/recording' },
    script:    { type: 'string' },
    seed:      { type: 'string' },  // warp to an intermediate state before recording
    duration:  { type: 'string', default: '60' },
    interval:  { type: 'string', default: '1000' },  // state capture interval ms
    width:     { type: 'string', default: '800' },
    height:    { type: 'string', default: '450' },
    headed:    { type: 'boolean', default: false },
    ...RUNTIME_IDENTITY_OPTIONS,
  },
});

if (!args.url) throw new Error('record-with-state requires --url');

const DURATION = Number(args.duration) * 1000;
const INTERVAL = Number(args.interval);
const WIDTH = Number(args.width);
const HEIGHT = Number(args.height);
const OUTPUT_DIR = resolve(args.output);

function percentile(values, p) {
  const nums = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!nums.length) return null;
  const idx = Math.min(nums.length - 1, Math.max(0, Math.ceil((p / 100) * nums.length) - 1));
  return nums[idx];
}

function sumObjectCount(sample) {
  if (!sample?.scenes) return 0;
  let count = 0;
  for (const scene of Object.values(sample.scenes)) {
    count += scene.objects?.length || 0;
  }
  return count;
}

function linearSlope(samples) {
  const points = samples.filter((s) => Number.isFinite(s.t) && Number.isFinite(s.value));
  if (points.length < 2) return 0;
  const meanT = points.reduce((sum, p) => sum + p.t, 0) / points.length;
  const meanV = points.reduce((sum, p) => sum + p.value, 0) / points.length;
  let numerator = 0;
  let denominator = 0;
  for (const p of points) {
    numerator += (p.t - meanT) * (p.value - meanV);
    denominator += (p.t - meanT) ** 2;
  }
  return denominator ? numerator / denominator : 0;
}

function buildPerformanceSummary({
  url,
  width,
  height,
  durationSeconds,
  intervalMs,
  bootSeconds,
  canMoveSeconds,
  timeline,
  browserMetrics,
}) {
  const fps = timeline.map((s) => s.fps).filter((v) => Number.isFinite(v) && v > 0);
  const objectCounts = timeline.map((s) => ({ t: s.t, value: sumObjectCount(s) }));
  const firstObjects = objectCounts.find((s) => Number.isFinite(s.value))?.value ?? null;
  const lastObjects = [...objectCounts].reverse().find((s) => Number.isFinite(s.value))?.value ?? null;
  const heapSamples = (browserMetrics.memorySamples || [])
    .filter((s) => Number.isFinite(s.usedJSHeapSize))
    .map((s) => ({ t: s.t, value: s.usedJSHeapSize / 1024 / 1024 }));
  const firstHeap = heapSamples[0]?.value ?? null;
  const lastHeap = heapSamples[heapSamples.length - 1]?.value ?? null;
  const longTasks = browserMetrics.longTasks || [];

  return {
    generated_at: new Date().toISOString(),
    url,
    viewport: { width, height },
    duration_seconds: durationSeconds,
    interval_ms: intervalMs,
    samples: {
      timeline: timeline.length,
      fps: fps.length,
      memory: heapSamples.length,
      long_tasks: longTasks.length,
    },
    timings: {
      boot_seconds: bootSeconds,
      can_move_seconds: canMoveSeconds,
      first_meaningful_frame_seconds: bootSeconds,
    },
    fps: {
      min: fps.length ? Math.min(...fps) : null,
      p05: percentile(fps, 5),
      p50: percentile(fps, 50),
      p95: percentile(fps, 95),
      avg: fps.length ? fps.reduce((sum, v) => sum + v, 0) / fps.length : null,
    },
    long_tasks: {
      count: longTasks.length,
      max_duration_ms: longTasks.length ? Math.max(...longTasks.map((t) => t.duration || 0)) : 0,
      total_duration_ms: longTasks.reduce((sum, t) => sum + (t.duration || 0), 0),
      tasks: longTasks.slice(0, 50),
    },
    memory: {
      supported: heapSamples.length > 0,
      first_used_js_heap_mb: firstHeap,
      last_used_js_heap_mb: lastHeap,
      growth_mb: firstHeap !== null && lastHeap !== null ? lastHeap - firstHeap : null,
      slope_mb_per_second: linearSlope(heapSamples),
    },
    objects: {
      first_count: firstObjects,
      last_count: lastObjects,
      growth_count: firstObjects !== null && lastObjects !== null ? lastObjects - firstObjects : null,
      slope_count_per_second: linearSlope(objectCounts),
    },
  };
}

// ── State extraction function (injected into browser) ────────────────────
const COMPACT_STATE_FN = function() {
  try {
    const game = window.__phaserGame;
    if (!game) return { error: 'no game' };

    const BUILTINS = new Set([
      'sys','game','anims','cache','registry','sound','textures','events',
      'cameras','add','make','scene','children','lights','data','input',
      'load','time','tweens','physics','matter','plugins','renderer','scale',
      'facebook','_events','_eventsCount','_maxListeners','init','preload','create','update',
    ]);

    const memory = performance.memory ? {
      usedJSHeapSize: performance.memory.usedJSHeapSize,
      totalJSHeapSize: performance.memory.totalJSHeapSize,
      jsHeapSizeLimit: performance.memory.jsHeapSizeLimit,
    } : null;
    const state = { ts: Date.now(), fps: Math.round(game.loop.actualFps), memory, scenes: {} };

    for (const scene of game.scene.getScenes(true)) {
      const key = scene.scene.key;
      const s = { groups: {}, custom: {}, objects: [] };

      // Object count + key objects
      if (scene.children?.list) {
        for (const obj of scene.children.list) {
          if (!obj.active) continue;
          const entry = { type: obj.type, x: Math.round(obj.x), y: Math.round(obj.y) };
          if (obj.texture?.key) entry.texture = obj.texture.key;
          if (obj.body) {
            entry.vx = Math.round(obj.body.velocity?.x || 0);
            entry.vy = Math.round(obj.body.velocity?.y || 0);
          }
          if (obj.name) entry.name = obj.name;
          s.objects.push(entry);
        }
      }

      // Groups
      for (const [prop, val] of Object.entries(scene)) {
        if (BUILTINS.has(prop) || prop.startsWith('_')) continue;
        if (val && typeof val.getChildren === 'function' && typeof val.countActive === 'function') {
          s.groups[prop] = { total: val.getLength(), active: val.countActive(true) };
        }
      }

      // Custom properties
      for (const [prop, val] of Object.entries(scene)) {
        if (BUILTINS.has(prop) || prop.startsWith('_')) continue;
        if (typeof val === 'function') continue;
        if (val && typeof val === 'object') {
          if (typeof val.getChildren === 'function') continue;
          if (val.scene && val.type) continue;
          if (val.manager && val.enabled !== undefined) continue;
          if (val.keyCode !== undefined) continue;
          if (val.up && val.up.keyCode !== undefined) continue;
          continue; // skip complex objects for compact timeline
        }
        if (['number', 'string', 'boolean'].includes(typeof val) || val === null) {
          s.custom[prop] = val;
        }
      }

      state.scenes[key] = s;
    }
    return state;
  } catch (e) { return { error: e.message }; }
};

// ── Seed: warp to an intermediate state before recording ──────────────────
// Same schema as the play-agent's objective.setup, so a scenario can be
// reused across verifiers. Lets a recording start from a reasonable mid-game
// state (boss fight, low HP, late level) instead of always booting fresh —
// state injection, never replay from boot.
function loadSeed(val) {
  if (!val) return null;
  try {
    if (existsSync(val)) return JSON.parse(readFileSync(val, 'utf-8'));
    return JSON.parse(val); // allow inline JSON
  } catch (e) {
    console.error(`Seed parse failed (${e.message}); recording from boot.`);
    return null;
  }
}

async function applySeed(page, seed) {
  if (!seed) return;
  try {
    if (seed.scene) {
      await page.evaluate((s) => {
        const g = window.__phaserGame; if (!g) return;
        (s.stop || []).forEach((k) => { try { g.scene.stop(k); } catch {} });
        g.scene.start(s.scene, s.data || {});
      }, seed);
      await page.waitForFunction(
        (key) => { const g = window.__phaserGame; return g && g.scene.getScenes(true).some((sc) => sc.scene.key === key); },
        seed.scene, { timeout: 8000 },
      ).catch(() => {});
      await page.waitForTimeout(300);
    }
    if (seed.state) {
      await page.evaluate((s) => {
        const g = window.__phaserGame; if (!g) return;
        for (const [scene, props] of Object.entries(s.state)) {
          const sc = g.scene.getScene(scene);
          if (sc) for (const [k, v] of Object.entries(props)) { try { sc[k] = v; } catch {} }
        }
      }, seed);
    }
    if (seed.snapshot) {
      // Full save-state restore via the game's __restore() hook (opt-in;
      // see references/verify/play-agent.md "Save-state snapshots").
      let snap = seed.snapshot;
      if (typeof snap === 'string') {
        try { snap = JSON.parse(readFileSync(resolve(snap), 'utf-8')); }
        catch (e) { console.error(`Snapshot load failed (${e.message})`); snap = null; }
      }
      if (snap) await page.evaluate((s) => {
        const g = window.__phaserGame;
        if (g && typeof g.__restore === 'function') g.__restore(s);
        else console.warn('[seed] game exposes no __restore(); snapshot ignored');
      }, snap);
    }
    if (seed.eval) await page.evaluate(`(()=>{ ${seed.eval} })()`);
    if (seed.js) {
      const mod = await import(resolve(seed.js));
      const fn = mod.default || mod.run;
      if (typeof fn === 'function') await fn(page);
    }
    await page.waitForTimeout(150);
    console.error(`Seed applied → ${seed.scene || 'custom state'}`);
  } catch (e) {
    console.error(`Seed apply failed: ${e.message}`);
  }
}

// ── Main ─────────────────────────────────────────────────────────────────

async function main() {
  mkdirSync(OUTPUT_DIR, { recursive: true });

  console.error(`Recording: ${args.url} (${WIDTH}x${HEIGHT}, ${DURATION/1000}s, state every ${INTERVAL}ms)`);

  const browser = await chromium.launch({ headless: !args.headed });
  const context = await browser.newContext({
    viewport: { width: WIDTH, height: HEIGHT },
    recordVideo: { dir: OUTPUT_DIR, size: { width: WIDTH, height: HEIGHT } },
  });
  const page = await context.newPage();
  await page.addInitScript(() => {
    window.__gcaPerf = {
      navigationStart: performance.now(),
      longTasks: [],
      memorySamples: [],
      canMoveAt: null,
    };
    try {
      const observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          window.__gcaPerf.longTasks.push({
            startTime: entry.startTime,
            duration: entry.duration,
            name: entry.name,
          });
        }
      });
      observer.observe({ entryTypes: ['longtask'] });
    } catch {}
  });

  // Navigate and wait for game
  const navStart = Date.now();
  await page.goto(args.url, { waitUntil: 'networkidle' });
  await verifyRuntimeIdentity(page, args);
  await page.waitForFunction(
    () => window.__phaserGame && window.__phaserGame.scene.getScenes(true).length > 0,
    { timeout: 15000 }
  ).catch(() => {});
  const bootSeconds = Number(((Date.now() - navStart) / 1000).toFixed(3));
  await page.waitForTimeout(1000);

  // Warp to the requested intermediate state before the capture window opens,
  // so the recording + performance/state timeline reflect the scenario, not boot.
  await applySeed(page, loadSeed(args.seed));

  const recordStart = Date.now();
  const timeline = [];

  console.error('Recording started. Capturing state...');

  // Start state capture loop
  const captureInterval = setInterval(async () => {
    try {
      const state = await page.evaluate(COMPACT_STATE_FN);
      const elapsed = ((Date.now() - recordStart) / 1000).toFixed(2);
      timeline.push({ t: Number(elapsed), ...state });
      await page.evaluate((t) => {
        if (!window.__gcaPerf) return;
        if (performance.memory) {
          window.__gcaPerf.memorySamples.push({
            t,
            usedJSHeapSize: performance.memory.usedJSHeapSize,
            totalJSHeapSize: performance.memory.totalJSHeapSize,
          });
        }
        if (window.__gcaCanMoveAt && !window.__gcaPerf.canMoveAt) {
          window.__gcaPerf.canMoveAt = window.__gcaCanMoveAt;
        }
      }, Number(elapsed));
    } catch {}
  }, INTERVAL);

  // Run interaction script or just wait
  if (args.script) {
    try {
      const mod = await import(resolve(args.script));
      const fn = mod.default || mod.run;
      if (typeof fn === 'function') {
        console.error(`Running script: ${args.script}`);
        await fn(page, browser);
      }
    } catch (e) {
      console.error(`Script error: ${e.message}`);
    }
  } else {
    await page.waitForTimeout(DURATION);
  }

  // Stop capture
  clearInterval(captureInterval);
  const finalRuntimeIdentity = await verifyRuntimeIdentity(page, args);

  // Final state capture
  try {
    const finalState = await page.evaluate(COMPACT_STATE_FN);
    timeline.push({ t: Number(((Date.now() - recordStart) / 1000).toFixed(2)), ...finalState });
  } catch {}

  // Save timeline
  const timelinePath = join(OUTPUT_DIR, 'state-timeline.json');
  writeFileSync(timelinePath, JSON.stringify(timeline, null, 2));
  console.error(`State timeline: ${timeline.length} snapshots → ${timelinePath}`);

  const browserMetrics = await page.evaluate(() => window.__gcaPerf || { longTasks: [], memorySamples: [] }).catch(() => ({ longTasks: [], memorySamples: [] }));
  const canMoveSeconds = Number.isFinite(browserMetrics.canMoveAt)
    ? Number((browserMetrics.canMoveAt / 1000).toFixed(3))
    : null;
  const perfSummary = buildPerformanceSummary({
    url: args.url,
    width: WIDTH,
    height: HEIGHT,
    durationSeconds: DURATION / 1000,
    intervalMs: INTERVAL,
    bootSeconds,
    canMoveSeconds,
    timeline,
    browserMetrics,
  });
  perfSummary.runtime_identity = finalRuntimeIdentity;
  const perfPath = join(OUTPUT_DIR, 'performance-summary.json');
  writeFileSync(perfPath, JSON.stringify(perfSummary, null, 2));
  console.error(`Performance summary: ${perfPath}`);

  // Close and finalize video
  const videoObj = page.video();
  await page.close();
  await context.close();

  if (videoObj) {
    try {
      const tmpPath = await videoObj.path();
      const finalPath = join(OUTPUT_DIR, 'recording.webm');
      if (tmpPath !== finalPath) {
        if (existsSync(finalPath)) unlinkSync(finalPath);
        renameSync(tmpPath, finalPath);
      }
      console.error(`Video: ${finalPath}`);
    } catch {}
  }

  await browser.close();
  console.log(JSON.stringify({
    schema_version: 1,
    source: 'record-with-state.mjs',
    url: args.url,
    runtime_identity: finalRuntimeIdentity,
    video: join(OUTPUT_DIR, 'recording.webm'),
    state_timeline: timelinePath,
    performance_summary: perfPath,
  }));
  console.error('Done!');
}

main().catch(err => { console.error('Error:', err.message); process.exit(1); });
