#!/usr/bin/env node
/**
 * test-runner.mjs — Playwright-based test runner for Phaser game verification.
 *
 * Runs test suites from .playtest/tests/ against a running game server.
 * Each test is an ES module exporting { name, tags?, covers?, setup, act, assert }.
 *
 * Usage:
 *   # Run test suite
 *   node test-runner.mjs --url http://localhost:8080 --tests .playtest/tests/
 *   node test-runner.mjs --url http://localhost:8080 --tests .playtest/tests/ --filter smoke
 *   node test-runner.mjs --url http://localhost:8080 --tests .playtest/tests/ --changed src/scenes/ArenaScene.js
 *   node test-runner.mjs --url http://localhost:8080 --tests .playtest/tests/ --id 003,007
 *   node test-runner.mjs --url http://localhost:8080 --tests .playtest/tests/ --screenshot-each
 *   node test-runner.mjs --url http://localhost:8080 --tests .playtest/tests/ --record
 *
 *   # Solo mode — screenshot / record without test files
 *   node test-runner.mjs --url http://localhost:8080 --solo
 *   node test-runner.mjs --url http://localhost:8080 --solo --script interact.mjs
 *   node test-runner.mjs --url http://localhost:8080 --solo --record --timeout 30000
 *   node test-runner.mjs --url http://localhost:8080 --solo --headed
 *
 * Flags:
 *   --screenshot-each   Capture before/after screenshots for every test
 *   --record            Record video (webm) of each test / solo session
 *   --solo              Solo mode — open game, optionally run script, capture output
 *   --script <path>     Interaction script for solo mode (exports default async fn(page))
 *   --headed            Visible browser window (solo mode only)
 *
 * Output: structured JSON results to stdout + human-readable summary to stderr.
 */

import { resolve, join, basename, relative } from 'node:path';
import { readdirSync, readFileSync, existsSync, writeFileSync, mkdirSync } from 'node:fs';
import { parseArgs } from 'node:util';

import { chromium } from './load-playwright.mjs';
import { RUNTIME_IDENTITY_OPTIONS, verifyRuntimeIdentity } from './runtime-identity.mjs';

// ── CLI args ─────────────────────────────────────────────────────────────
const { values: args } = parseArgs({
  options: {
    url:     { type: 'string', default: 'http://localhost:8080' },
    tests:   { type: 'string', default: '.playtest/tests' },
    filter:  { type: 'string' },     // filter by tag (e.g., "smoke", "unit")
    changed: { type: 'string' },     // comma-separated changed files for incremental runs
    id:      { type: 'string' },     // comma-separated test IDs to run
    timeout: { type: 'string', default: '15000' },
    width:   { type: 'string', default: '960' },
    height:  { type: 'string', default: '640' },
    screenshots: { type: 'string', default: '.playtest/verify/test-run/screenshots' },
    json:    { type: 'boolean', default: false },  // output JSON only
    manifest:{ type: 'string' },     // path to manifest.json (auto-detected if omitted)
    // ── Visual capture flags ─────────────────────────────────────────
    record:  { type: 'boolean', default: false },  // record video per test
    'screenshot-each': { type: 'boolean', default: false },  // before+after screenshots for every test
    recordings: { type: 'string', default: '.playtest/verify/test-run/recordings' },
    // ── Solo mode ────────────────────────────────────────────────────
    solo:    { type: 'boolean', default: false },  // no test files, just open + capture
    script:  { type: 'string' },                   // interaction script for solo mode
    headed:  { type: 'boolean', default: false },  // visible browser (solo mode)
    ...RUNTIME_IDENTITY_OPTIONS,
  },
});

const TIMEOUT = Number(args.timeout);
const WIDTH = Number(args.width);
const HEIGHT = Number(args.height);
const testsDir = resolve(args.tests);
const screenshotsDir = resolve(args.screenshots);
const recordingsDir = resolve(args.recordings);
const RECORD = args.record;
const SCREENSHOT_EACH = args['screenshot-each'];

// ── Discover tests ───────────────────────────────────────────────────────

function discoverTests(dir) {
  const tests = [];
  if (!existsSync(dir)) return tests;

  function walk(d) {
    for (const entry of readdirSync(d, { withFileTypes: true })) {
      if (entry.isDirectory()) {
        walk(join(d, entry.name));
      } else if (entry.name.endsWith('.js') || entry.name.endsWith('.mjs')) {
        tests.push(join(d, entry.name));
      }
    }
  }
  walk(dir);
  return tests.sort();
}

// ── Load manifest for incremental filtering ──────────────────────────────

function loadManifest() {
  const manifestPath = args.manifest
    ? resolve(args.manifest)
    : join(testsDir, 'manifest.json');

  if (!existsSync(manifestPath)) return null;
  try {
    return JSON.parse(readFileSync(manifestPath, 'utf-8'));
  } catch {
    return null;
  }
}

// ── Filter tests ─────────────────────────────────────────────────────────

function shouldRun(testMod, testPath, manifest) {
  // Filter by ID
  if (args.id) {
    const ids = args.id.split(',').map(s => s.trim());
    const fileId = basename(testPath).match(/^(\d+)/)?.[1];
    const modId = testMod.id;
    if (!ids.includes(fileId) && !ids.includes(modId)) return false;
  }

  // Filter by tag
  if (args.filter) {
    const tags = testMod.tags || [];
    const dir = relative(testsDir, testPath).split('/')[0]; // directory name as implicit tag
    if (!tags.includes(args.filter) && dir !== args.filter) return false;
  }

  // Filter by changed files (incremental)
  if (args.changed && manifest) {
    const changedFiles = args.changed.split(',').map(s => s.trim());
    const testEntry = manifest.tests?.find(t =>
      resolve(testsDir, '..', t.file) === testPath || t.file === relative(join(testsDir, '..'), testPath)
    );
    if (testEntry && testEntry.covers) {
      const covers = testEntry.covers;
      const affected = changedFiles.some(f =>
        covers.some(c => f.endsWith(c) || c.endsWith(f) || f.includes(c) || c.includes(f))
      );
      if (!affected) return false;
    }
    // If no manifest entry for this test, run it anyway (safety)
  }

  return true;
}

// ── Run one test ─────────────────────────────────────────────────────────

async function runTest(browser, testModule, testPath) {
  const name = testModule.name || basename(testPath, '.js');
  const safeName = name.replace(/[^a-zA-Z0-9_-]/g, '_');
  const startTime = Date.now();
  let page, context, runtimeIdentity;

  try {
    // ── Context setup: optional video recording ──────────────────────
    const contextOpts = { viewport: { width: WIDTH, height: HEIGHT } };

    if (RECORD) {
      mkdirSync(recordingsDir, { recursive: true });
      contextOpts.recordVideo = {
        dir: resolve(recordingsDir, '.tmp-videos'),
        size: { width: WIDTH, height: HEIGHT },
      };
      mkdirSync(resolve(recordingsDir, '.tmp-videos'), { recursive: true });
    }

    context = await browser.newContext(contextOpts);
    page = await context.newPage();

    // Install error capture
    await page.addInitScript(() => {
      window.__gameVerifyErrors = [];
      const origErr = console.error;
      console.error = function (...a) {
        window.__gameVerifyErrors.push(a.map(String).join(' '));
        origErr.apply(console, a);
      };
      window.addEventListener('error', e => {
        window.__gameVerifyErrors.push(`[uncaught] ${e.message}`);
      });
    });

    // Navigate
    await page.goto(args.url, { waitUntil: 'networkidle' });
    runtimeIdentity = await verifyRuntimeIdentity(page, args);

    // Wait for Phaser to boot
    await page.waitForFunction(
      () => window.__phaserGame && window.__phaserGame.scene.getScenes(true).length > 0,
      { timeout: TIMEOUT }
    ).catch(() => {});

    await page.waitForTimeout(500);

    // ── Setup ────────────────────────────────────────────────────────
    if (testModule.setup) {
      await Promise.race([
        testModule.setup(page),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('setup timeout')), TIMEOUT)
        ),
      ]);
    }

    // ── Before screenshot ────────────────────────────────────────────
    let beforeScreenshot;
    if (SCREENSHOT_EACH) {
      mkdirSync(screenshotsDir, { recursive: true });
      beforeScreenshot = join(screenshotsDir, `${safeName}_before.png`);
      await page.screenshot({ path: beforeScreenshot });
    }

    // ── Act ──────────────────────────────────────────────────────────
    if (testModule.act) {
      await Promise.race([
        testModule.act(page),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('act timeout')), TIMEOUT)
        ),
      ]);
    }

    // ── After screenshot ─────────────────────────────────────────────
    let afterScreenshot;
    if (SCREENSHOT_EACH) {
      afterScreenshot = join(screenshotsDir, `${safeName}_after.png`);
      await page.screenshot({ path: afterScreenshot });
    }

    // ── Assert ───────────────────────────────────────────────────────
    if (testModule.assert) {
      await Promise.race([
        testModule.assert(page),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('assert timeout')), TIMEOUT)
        ),
      ]);
    }
    runtimeIdentity = await verifyRuntimeIdentity(page, args);

    // Collect console errors
    const gameErrors = await page.evaluate(() => window.__gameVerifyErrors || []);

    // ── Finalize video ───────────────────────────────────────────────
    let videoPath;
    if (RECORD) {
      const videoObj = page.video();
      await page.close();
      await context.close();
      if (videoObj) {
        try {
          const tmpPath = await videoObj.path();
          videoPath = join(recordingsDir, `${safeName}.webm`);
          const { renameSync, existsSync: ex } = await import('fs');
          if (ex(tmpPath)) renameSync(tmpPath, videoPath);
        } catch { /* video save failed */ }
      }
    } else {
      await context.close();
    }

    const result = {
      name,
      id: testModule.id || basename(testPath).match(/^(\d+)/)?.[1] || name,
      file: relative(process.cwd(), testPath),
      status: 'PASS',
      duration: Date.now() - startTime,
      browser_opened: true,
      runtime_identity: runtimeIdentity,
      consoleErrors: gameErrors.length > 0 ? gameErrors : undefined,
    };
    if (beforeScreenshot) result.screenshots = {
      before: relative(process.cwd(), beforeScreenshot),
      after: relative(process.cwd(), afterScreenshot),
    };
    if (videoPath) result.video = relative(process.cwd(), videoPath);
    return result;

  } catch (err) {
    // ── Failure: capture screenshot + finalize video ─────────────────
    let failScreenshot, videoPath;
    if (page) {
      try {
        runtimeIdentity = await verifyRuntimeIdentity(page, args);
      } catch {
        runtimeIdentity = null;
      }
      try {
        mkdirSync(screenshotsDir, { recursive: true });
        failScreenshot = join(screenshotsDir, `FAIL_${safeName}.png`);
        await page.screenshot({ path: failScreenshot });
      } catch { /* can't screenshot */ }

      if (RECORD) {
        try {
          const videoObj = page.video();
          await page.close();
          await context.close();
          if (videoObj) {
            const tmpPath = await videoObj.path();
            videoPath = join(recordingsDir, `FAIL_${safeName}.webm`);
            const { renameSync, existsSync: ex } = await import('fs');
            if (ex(tmpPath)) renameSync(tmpPath, videoPath);
          }
        } catch { /* video save failed */ }
      } else {
        try { await context.close(); } catch {}
      }
    }

    const result = {
      name,
      id: testModule.id || basename(testPath).match(/^(\d+)/)?.[1] || name,
      file: relative(process.cwd(), testPath),
      status: 'FAIL',
      error: err.message,
      duration: Date.now() - startTime,
      browser_opened: Boolean(page),
      runtime_identity: runtimeIdentity,
    };
    if (failScreenshot) result.screenshot = relative(process.cwd(), failScreenshot);
    if (videoPath) result.video = relative(process.cwd(), videoPath);
    return result;
  }
}

// ── Solo mode — open game, run optional script, screenshot/record ────────

async function runSolo() {
  const outputDir = resolve(args.screenshots);
  mkdirSync(outputDir, { recursive: true });

  const contextOpts = { viewport: { width: WIDTH, height: HEIGHT } };
  if (RECORD) {
    mkdirSync(recordingsDir, { recursive: true });
    contextOpts.recordVideo = {
      dir: resolve(recordingsDir, '.tmp-videos'),
      size: { width: WIDTH, height: HEIGHT },
    };
    mkdirSync(resolve(recordingsDir, '.tmp-videos'), { recursive: true });
  }

  const browser = await chromium.launch({ headless: !args.headed });
  const context = await browser.newContext(contextOpts);
  const page = await context.newPage();

  // Error capture
  const jsErrors = [];
  page.on('pageerror', (err) => jsErrors.push(err.message));
  page.on('console', (msg) => { if (msg.type() === 'error') jsErrors.push(msg.text()); });

  console.error(`Opening ${args.url} (${WIDTH}x${HEIGHT}, ${args.headed ? 'headed' : 'headless'})...`);
  await page.goto(args.url, { waitUntil: 'networkidle' });
  const runtimeIdentity = await verifyRuntimeIdentity(page, args);
  await page.waitForTimeout(2000);

  // Initial screenshot
  const shotBefore = join(outputDir, 'solo_initial.png');
  await page.screenshot({ path: shotBefore });
  console.error(`  screenshot: ${relative(process.cwd(), shotBefore)}`);

  // Run interaction script if provided
  if (args.script) {
    try {
      const mod = await import(resolve(args.script));
      const fn = mod.default || mod.run;
      if (typeof fn === 'function') {
        console.error(`  running script: ${args.script}`);
        await fn(page, browser);
      } else {
        console.error(`  script must export default async function(page, browser)`);
      }
    } catch (err) {
      console.error(`  script error: ${err.message}`);
    }
  } else {
    // No script — just wait
    console.error(`  no --script, waiting ${TIMEOUT}ms...`);
    await page.waitForTimeout(TIMEOUT);
  }
  await verifyRuntimeIdentity(page, args);

  // Final screenshot
  const shotAfter = join(outputDir, 'solo_final.png');
  await page.screenshot({ path: shotAfter });
  console.error(`  screenshot: ${relative(process.cwd(), shotAfter)}`);

  // Extract game state
  let gameState;
  try {
    gameState = await page.evaluate(() => {
      const g = window.__phaserGame;
      if (!g) return null;
      return {
        fps: Math.round(g.loop.actualFps),
        activeScenes: g.scene.getScenes(true).map(s => s.scene.key),
        objectCount: g.scene.getScenes(true).reduce((n, s) => n + (s.children?.list?.length || 0), 0),
      };
    });
  } catch {}

  // Finalize video
  let videoPath;
  if (RECORD) {
    const videoObj = page.video();
    await page.close();
    await context.close();
    if (videoObj) {
      try {
        const tmpPath = await videoObj.path();
        videoPath = join(recordingsDir, 'solo_recording.webm');
        const { renameSync, existsSync: ex } = await import('fs');
        if (ex(tmpPath)) renameSync(tmpPath, videoPath);
        console.error(`  video: ${relative(process.cwd(), videoPath)}`);
      } catch {}
    }
    try {
      const { rmSync } = await import('fs');
      rmSync(resolve(recordingsDir, '.tmp-videos'), { recursive: true, force: true });
    } catch {}
  } else {
    await context.close();
  }

  await browser.close();

  // Output
  const result = {
    mode: 'solo',
    url: args.url,
    screenshots: {
      before: relative(process.cwd(), shotBefore),
      after: relative(process.cwd(), shotAfter),
    },
    video: videoPath ? relative(process.cwd(), videoPath) : undefined,
    script: args.script || null,
    gameState,
    jsErrors: jsErrors.length > 0 ? jsErrors : undefined,
    duration: Date.now(),
    runtime_identity: runtimeIdentity,
  };

  console.log(JSON.stringify(result, null, 2));

  if (jsErrors.length) {
    console.error(`\n  JS errors (${jsErrors.length}):`);
    jsErrors.forEach(e => console.error(`    ${e}`));
  }
}

// ── Main ─────────────────────────────────────────────────────────────────

async function main() {
  // Solo mode — bypass test discovery
  if (args.solo) {
    await runSolo();
    return;
  }

  const testPaths = discoverTests(testsDir);
  if (testPaths.length === 0) {
    console.error('No tests found in', testsDir);
    console.log(JSON.stringify({ mode: 'formal', tests: [], summary: { total: 0, pass: 0, fail: 0, skip: 0 } }));
    process.exit(1);
  }

  const manifest = loadManifest();
  const browser = await chromium.launch({ headless: true });
  const results = [];
  let skipCount = 0;

  for (const testPath of testPaths) {
    let testModule;
    try {
      testModule = await import(`file://${testPath}`);
      testModule = testModule.default || testModule;
    } catch (err) {
      results.push({
        name: basename(testPath),
        file: relative(process.cwd(), testPath),
        status: 'FAIL',
        error: `Failed to import test: ${err.message}`,
        duration: 0,
      });
      continue;
    }

    if (!shouldRun(testModule, testPath, manifest)) {
      skipCount++;
      continue;
    }

    // A module with no phases would open the page, do nothing, and "pass" —
    // fail it explicitly instead of reporting a vacuous PASS.
    if (!testModule.setup && !testModule.act && !testModule.assert) {
      results.push({
        name: testModule.name || basename(testPath),
        file: relative(process.cwd(), testPath),
        status: 'FAIL',
        error: 'test module defines none of setup/act/assert — nothing would be exercised',
        duration: 0,
      });
      continue;
    }

    if (!args.json) {
      process.stderr.write(`  Running: ${testModule.name || basename(testPath)}...`);
    }

    const result = await runTest(browser, testModule, testPath);
    results.push(result);

    if (!args.json) {
      const icon = result.status === 'PASS' ? '\x1b[32mPASS\x1b[0m' : '\x1b[31mFAIL\x1b[0m';
      process.stderr.write(` ${icon} (${result.duration}ms)\n`);
      if (result.error) {
        process.stderr.write(`    Error: ${result.error}\n`);
      }
    }
  }

  await browser.close();

  // Clean up temp video dir
  if (RECORD) {
    try {
      const { rmSync } = await import('fs');
      rmSync(resolve(recordingsDir, '.tmp-videos'), { recursive: true, force: true });
    } catch {}
  }

  // ── Summary ──────────────────────────────────────────────────────────
  const pass = results.filter(r => r.status === 'PASS').length;
  const fail = results.filter(r => r.status === 'FAIL').length;
  const total = results.length;

  const summary = { total, pass, fail, skip: skipCount, duration: results.reduce((s, r) => s + r.duration, 0) };
  const runtimeIdentity = results.find((result) => result.runtime_identity)?.runtime_identity;
  const output = {
    mode: 'formal',
    tests: results,
    summary,
    runtime_identity: runtimeIdentity,
  };

  if (args.json) {
    console.log(JSON.stringify(output, null, 2));
  } else {
    process.stderr.write(`\n${'─'.repeat(50)}\n`);
    process.stderr.write(`  Total: ${total}  Pass: \x1b[32m${pass}\x1b[0m  Fail: \x1b[31m${fail}\x1b[0m  Skip: ${skipCount}\n`);
    if (fail > 0) {
      process.stderr.write(`\n  Failed tests:\n`);
      for (const r of results.filter(r => r.status === 'FAIL')) {
        process.stderr.write(`    \x1b[31m✗\x1b[0m ${r.name}: ${r.error}\n`);
        if (r.screenshot) process.stderr.write(`      screenshot: ${r.screenshot}\n`);
      }
    }
    process.stderr.write(`${'─'.repeat(50)}\n`);
    // Also output JSON for programmatic consumption
    console.log(JSON.stringify(output, null, 2));
  }

  process.exit(fail > 0 ? 1 : 0);
}

main().catch(err => {
  console.error('Runner error:', err);
  process.exit(2);
});
