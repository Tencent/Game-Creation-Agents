#!/usr/bin/env node
/**
 * play-agent.mjs — intelligent closed-loop play-agent for game verification.
 *
 * Instead of a blind pre-written interaction script, this drives the game in a
 * perceive -> decide -> act -> observe loop (VideoGameBench-style ReAct):
 *   1. (optionally) pause the Phaser loop so slow LLM latency can't invalidate
 *      the chosen action ("pause while thinking", VideoGameBench-Lite trick)
 *   2. observe: screenshot + structured state (state-extractor, gray-box)
 *   3. decide: VLM/LLM emits { thought, action, args, memory, status }
 *   4. act: actuate the action through the interaction contract's actions (keyboard/pointer)
 *   5. observe again, run bug oracles, log trajectory
 * Per objective it then judges YES / NO / PARTIAL (WebGen-Bench style) and the
 * report scores weighted accuracy. Always degrades to a heuristic policy if no
 * LLM is reachable, so the harness still runs end-to-end.
 *
 * Usage:
 *   node play-agent.mjs --url http://localhost:8080 --game playground/fish-eater
 *   node play-agent.mjs --url ... --game ... --policy heuristic        # no LLM
 *   node play-agent.mjs --url ... --game ... --vision-only             # drop state text
 *   node play-agent.mjs --url ... --game ... --realtime                # don't pause loop
 *   node play-agent.mjs --url ... --game ... --record --headed
 *
 * Inputs (under <game>/.playtest/inputs/; legacy verify/play/ still resolves):
 *   interaction-contract.json   semantic action -> input primitive mapping (actions block)
 *   objectives.json    { schema_version: 1, objectives: [{ id, operation, expected_result, ... }] }
 *
 * Outputs (under <game>/.playtest/verify/play/):
 *   trajectory.jsonl       append-only per-step record
 *   agent-play-report.json objectives verdicts + weighted score + bugs + build health
 *   frames/                screenshots per step
 */

import { resolve, join } from 'node:path';
import { readFileSync, writeFileSync, existsSync, mkdirSync, appendFileSync, statSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';

import { extractState, extractStateCompact, installErrorCapture, waitForGame } from './state-extractor.mjs';
import { runStepOracles, stateSignature, timingOracle } from './oracles.mjs';
import { callModel, extractJson, detectProvider } from './llm-client.mjs';
import { assertObjectivesDocument } from './objectives-contract.mjs';
import { resolveRead, CONTROL_MAP as CONTROL_MAP_REL, OBJECTIVES as OBJECTIVES_REL } from './playtest-layout.mjs';
import { RUNTIME_IDENTITY_OPTIONS, verifyRuntimeIdentity } from './runtime-identity.mjs';

// ── CLI ──────────────────────────────────────────────────────────────────
const { values: args } = parseArgs({
  options: {
    url: { type: 'string', default: 'http://localhost:8080' },
    game: { type: 'string', default: '.' },
    'control-map': { type: 'string' },
    objectives: { type: 'string' },
    out: { type: 'string' },
    policy: { type: 'string', default: 'llm' },        // 'llm' | 'heuristic'
    model: { type: 'string' },
    provider: { type: 'string' },
    'vision-only': { type: 'boolean', default: false },
    realtime: { type: 'boolean', default: false },      // disable pause-while-thinking
    headed: { type: 'boolean', default: false },
    record: { type: 'boolean', default: false },
    'max-steps': { type: 'string', default: '30' },     // per-objective default cap
    'action-delay': { type: 'string', default: '450' }, // ms to let game run after each action
    'think-budget': { type: 'string', default: '3072' }, // decide max_tokens (reasoning models need headroom)
    width: { type: 'string', default: '800' },
    height: { type: 'string', default: '600' },
    seeds: { type: 'string', default: '1' },            // repeat-for-stochasticity
    'max-wall': { type: 'string', default: '240' },     // hard wall-clock cap (seconds)
    'nav-timeout': { type: 'string', default: '20000' },
    changed: { type: 'string' },                        // diff-aware: comma-sep changed files
    'regions': { type: 'string', default: '4' },        // spatial coverage grid (NxN)
    ...RUNTIME_IDENTITY_OPTIONS,
  },
});

const GAME_DIR = resolve(args.game);
const PLAY_DIR = resolve(args.out || join(GAME_DIR, '.playtest', 'verify', 'play'));
const FRAMES_DIR = join(PLAY_DIR, 'frames');
// Play-maps are INPUTS authored by scaffold → .playtest/inputs/ (with legacy fallback).
const PLAYTEST_DIR = join(GAME_DIR, '.playtest');
const CONTROL_MAP_PATH = resolve(args['control-map'] || resolveRead(PLAYTEST_DIR, CONTROL_MAP_REL));
const OBJECTIVES_PATH = resolve(args.objectives || resolveRead(PLAYTEST_DIR, OBJECTIVES_REL));
const TRAJECTORY_PATH = join(PLAY_DIR, 'trajectory.jsonl');
const REPORT_PATH = join(PLAY_DIR, 'agent-play-report.json');
const RUN_ID = `play-${Date.now()}-${process.pid}`;
const DEFAULT_MAX_STEPS = Number(args['max-steps']);
const ACTION_DELAY = Number(args['action-delay']);
const WIDTH = Number(args.width);
const HEIGHT = Number(args.height);
const SEEDS = Math.max(1, Number(args.seeds));
const REALTIME = args.realtime;
const VISION_ONLY = args['vision-only'];
const NAV_TIMEOUT = Number(args['nav-timeout']);
const MAX_WALL_MS = Number(args['max-wall']) * 1000;
const REPORT_GRACE_MS = 10000;
const MODEL_TIMEOUT_SECONDS = 120;
// Soft deadline: stop starting decisions before the hard watchdog so the
// current objective can still be judged and the report written.
let DEADLINE = Infinity;
let HARD_DEADLINE = Infinity;
let ACTIVE_OBJECTIVE = null;

// Fast-fail guards against burning steps (each ~one LLM call) on actions that can
// never land. A structural error (unknown primitive/action) is a contract/impl defect
// that affects EVERY objective → set once, main() then skips the rest. A run of
// ordinary failed actuations means inputs aren't reaching the game → end that objective.
const MAX_CONSECUTIVE_FAILS = 4;
let STRUCTURAL_ACTUATION_ERROR = false;

function log(...a) { console.error(...a); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function boundedModelTimeout(deadline) {
  return Math.max(
    0,
    Math.min(
      MODEL_TIMEOUT_SECONDS,
      Math.floor((deadline - Date.now()) / 1000),
    ),
  );
}

// ── Load contracts ─────────────────────────────────────────────────────
function loadJson(path, fallback) {
  if (!existsSync(path)) return fallback;
  try { return JSON.parse(readFileSync(path, 'utf-8')); } catch (e) { log(`  ! failed to parse ${path}: ${e.message}`); return fallback; }
}

const controlMap = loadJson(CONTROL_MAP_PATH, null);
let objectivesDoc = null;
if (!existsSync(OBJECTIVES_PATH)) {
  log(`Error: objectives contract not found at ${OBJECTIVES_PATH}`);
  log('Scaffold must generate the canonical strict objectives.json before play.');
  process.exit(2);
} else {
  try {
    objectivesDoc = assertObjectivesDocument(
      JSON.parse(readFileSync(OBJECTIVES_PATH, 'utf-8')),
      OBJECTIVES_PATH,
    );
  } catch (error) {
    log(`Error: ${error.message}`);
    process.exit(2);
  }
}

if (!controlMap || !controlMap.actions) {
  log(`Error: control-map not found or invalid at ${CONTROL_MAP_PATH}`);
  log(`Create it first (see references/play-agent.md for the schema).`);
  process.exit(2);
}
let OBJECTIVES = objectivesDoc.objectives;

// Diff-aware (SAGE/SMART): when --changed lists changed files, prioritize
// objectives whose declared `scenes` are touched. Objectives without a `scenes`
// field always run (can't prove they're unaffected).
const CHANGED_FILES = (args.changed || '').split(',').map((s) => s.trim()).filter(Boolean);
if (CHANGED_FILES.length) {
  const before = OBJECTIVES.length;
  OBJECTIVES = OBJECTIVES.filter((o) => {
    if (!o.scenes || !o.scenes.length) return true;
    return o.scenes.some((sc) => CHANGED_FILES.some((f) => f.toLowerCase().includes(String(sc).toLowerCase())));
  });
  log(`  diff-aware: ${OBJECTIVES.length}/${before} objectives selected for changed files [${CHANGED_FILES.join(', ')}]`);
}
const REGIONS_N = Math.max(1, Number(args.regions));

const DESIGN_W = controlMap.canvas?.designWidth || WIDTH;
const DESIGN_H = controlMap.canvas?.designHeight || HEIGHT;
const GAME_CONTEXT = controlMap.gameContext || '';
// Keep contract errors independent from Playwright installation/browser work.
const { chromium } = await import('./load-playwright.mjs');

// ── Decide provider / policy ───────────────────────────────────────────
let POLICY = args.policy;
if (POLICY === 'llm') {
  const prov = detectProvider({ provider: args.provider, model: args.model });
  if (!prov) { log('  ! No LLM provider available — falling back to heuristic policy.'); POLICY = 'heuristic'; }
  else log(`  LLM provider: ${prov}`);
}

// ── Pointer / coordinate resolution ────────────────────────────────────
async function getCanvasBox(page) {
  const canvas = await page.locator('canvas').first();
  const box = await canvas.boundingBox();
  return box || { x: 0, y: 0, width: WIDTH, height: HEIGHT };
}
function resolveCoord(val, axis, box) {
  // axis: 'x' | 'y'. Accepts number (game-space), "NN%", "center".
  const designDim = axis === 'x' ? DESIGN_W : DESIGN_H;
  const boxOrigin = axis === 'x' ? box.x : box.y;
  const boxSize = axis === 'x' ? box.width : box.height;
  if (val === 'center' || val === undefined || val === null) return boxOrigin + boxSize / 2;
  if (typeof val === 'string' && val.trim().endsWith('%')) {
    return boxOrigin + (parseFloat(val) / 100) * boxSize;
  }
  const n = Number(val);
  if (!Number.isFinite(n)) return boxOrigin + boxSize / 2;
  return boxOrigin + (n / designDim) * boxSize; // game-space -> page-space
}

// ── Actuate one semantic action via the control-map ─────────────────────
async function actuate(page, actionName, llmArgs = {}) {
  const spec = controlMap.actions[actionName];
  if (!spec) return { ok: false, note: `unknown action '${actionName}'` };
  const prim = spec.primitive;
  const box = await getCanvasBox(page);
  try {
    // Pointer primitives. `tap` is an alias for a full click; `pointer_down`/
    // `pointer_up` are the press/release halves (for hold/drag gestures) — a contract
    // that maps a tap-style action to `pointer_down` used to fail with "unknown
    // primitive" and never reach the game (title-screen START buttons, etc.). We treat
    // a bare `pointer_down` as a full tap so a single press-style action still clicks.
    if (prim === 'pointer_move' || prim === 'pointer_click' || prim === 'tap'
        || prim === 'pointer_down' || prim === 'pointer_up') {
      // x/y come from spec or from LLM args (argsFromLLM lists the param names)
      const fromLLM = spec.argsFromLLM || [];
      const xVal = fromLLM.includes('x') ? llmArgs.x : (spec.x ?? 'center');
      const yVal = fromLLM.includes('y') ? llmArgs.y : (spec.y ?? 'center');
      const px = resolveCoord(xVal, 'x', box);
      const py = resolveCoord(yVal, 'y', box);
      const button = (spec.button === 'right' || spec.button === 'middle') ? spec.button : 'left';
      await page.mouse.move(px, py);
      if (prim === 'pointer_click' || prim === 'tap' || prim === 'pointer_down') {
        await page.mouse.down({ button }); await sleep(40); await page.mouse.up({ button });
      } else if (prim === 'pointer_up') {
        await page.mouse.up({ button });
      }
      return { ok: true, resolved: { x: Math.round(px), y: Math.round(py), button } };
    }
    if (prim === 'key_press') {
      const key = spec.key;
      if (spec.hold_ms) { await page.keyboard.down(key); await sleep(spec.hold_ms); await page.keyboard.up(key); }
      else await page.keyboard.press(key);
      return { ok: true, resolved: { key } };
    }
    if (prim === 'key_down') { await page.keyboard.down(spec.key); return { ok: true, resolved: { down: spec.key } }; }
    if (prim === 'key_up') { await page.keyboard.up(spec.key); return { ok: true, resolved: { up: spec.key } }; }
    if (prim === 'wait') { await sleep(spec.ms || ACTION_DELAY); return { ok: true, resolved: { wait: spec.ms || ACTION_DELAY } }; }
    return { ok: false, note: `unknown primitive '${prim}'` };
  } catch (e) {
    return { ok: false, note: `actuation error: ${e.message}` };
  }
}

// ── Pause / resume the Phaser game loop (pause-while-thinking) ───────────
async function pauseGame(page) {
  if (REALTIME) return;
  await page.evaluate(() => {
    const g = window.__phaserGame;
    if (!g || !g.loop) return;
    try { if (typeof g.loop.sleep === 'function') g.loop.sleep(); else g.loop.running = false; } catch {}
  }).catch(() => {});
}
async function resumeGame(page) {
  if (REALTIME) return;
  await page.evaluate(() => {
    const g = window.__phaserGame;
    if (!g || !g.loop) return;
    try { if (typeof g.loop.wake === 'function') g.loop.wake(); else { g.loop.running = true; g.loop.step?.(performance.now()); } } catch {}
  }).catch(() => {});
}

// ── Warp / state injection (skip replaying from boot) ────────────────────
// An objective's optional `setup` jumps straight to the state under test, so we
// don't waste steps clicking through menus/levels. Mirrors test-runner's setup
// (state injection, never replay from boot). Fields (all optional):
//   scene  : scene key to start (e.g. "GameScene")
//   stop   : [scene keys] to stop first (e.g. ["TitleScene"])
//   data   : object passed to scene.start(scene, data)  (e.g. { level: 5 })
//   state  : { SceneKey: { prop: value, ... } } shallow-assigned after start
//            (e.g. set score/size/lives to reach a precondition)
//   eval   : JS statements run in the page (advanced: spawn/position entities)
//   js     : path to a module exporting default async (page) for complex setup
// NOTE: setup establishes the PRECONDITION; the objective's success must still
// be earned by play (don't inject the success state itself).
async function applySetup(page, setup) {
  if (!setup) return { ok: true, applied: false };
  try {
    if (setup.scene) {
      await page.evaluate((s) => {
        const g = window.__phaserGame; if (!g) return;
        (s.stop || []).forEach((k) => { try { g.scene.stop(k); } catch {} });
        g.scene.start(s.scene, s.data || {});
      }, setup);
      await page.waitForFunction(
        (key) => { const g = window.__phaserGame; return g && g.scene.getScenes(true).some((sc) => sc.scene.key === key); },
        setup.scene, { timeout: 8000 },
      ).catch(() => {});
      await page.waitForTimeout(300); // let create() finish
    }
    if (setup.state) {
      await page.evaluate((s) => {
        const g = window.__phaserGame; if (!g) return;
        for (const [scene, props] of Object.entries(s.state)) {
          const sc = g.scene.getScene(scene);
          if (sc) for (const [k, v] of Object.entries(props)) { try { sc[k] = v; } catch {} }
        }
      }, setup);
    }
    if (setup.snapshot) {
      // Full save-state restore via the game's __restore() hook (opt-in;
      // see references/verify/play-agent.md "Save-state snapshots").
      let snap = setup.snapshot;
      if (typeof snap === 'string') {
        try { snap = JSON.parse(readFileSync(resolve(snap), 'utf-8')); }
        catch (e) { log(`  setup: snapshot load failed (${e.message})`); snap = null; }
      }
      if (snap) await page.evaluate((s) => {
        const g = window.__phaserGame;
        if (g && typeof g.__restore === 'function') g.__restore(s);
        else console.warn('[setup] game exposes no __restore(); snapshot ignored');
      }, snap);
    }
    if (setup.eval) {
      await page.evaluate(`(()=>{ ${setup.eval} })()`);
    }
    if (setup.js) {
      const mod = await import(resolve(setup.js));
      const fn = mod.default || mod.run;
      if (typeof fn === 'function') await fn(page);
    }
    await page.waitForTimeout(150);
    return { ok: true, applied: true };
  } catch (e) {
    return { ok: false, applied: true, error: e.message };
  }
}

// ── Build the action menu string for the prompt ─────────────────────────
function actionMenu() {
  return Object.entries(controlMap.actions).map(([name, spec]) => {
    const params = (spec.argsFromLLM || []).length ? ` (args: ${spec.argsFromLLM.join(', ')})` : '';
    return `- "${name}"${params}: ${spec.description || spec.primitive}`;
  }).join('\n');
}

// ── Summarize on-screen actors from full state (gray-box spatial signal) ──
// Game-agnostic: lists active objects with texture, position, and any
// game-specific data (e.g. sizeCategory). This is what lets the agent target
// precisely instead of guessing from a small screenshot.
function summarizeActors(fullState) {
  if (!fullState || fullState.error) return '(unavailable)';
  const lines = [];
  for (const [sceneKey, scene] of Object.entries(fullState.scenes || {})) {
    const objs = (scene.objects || []).filter((o) => o.active && o.visible && (o.texture || o.type));
    if (!objs.length) continue;
    const rows = objs.slice(0, 30).map((o) => {
      const tag = o.texture || o.type;
      const data = o.data ? ' ' + JSON.stringify(o.data) : '';
      const sz = o.scale ? ` scale~${o.scale.x}` : '';
      return `  ${tag} @(${Math.round(o.x)},${Math.round(o.y)})${sz}${data}`;
    });
    lines.push(`${sceneKey}:\n${rows.join('\n')}`);
  }
  return lines.join('\n') || '(no visible objects)';
}

// ── Coverage helpers ─────────────────────────────────────────────────────
// Best-effort identification of the controllable object — used ONLY for spatial
// region coverage (every other part of the agent is fully game-agnostic).
// Prefer an explicit control-map hint (`player`: a texture/name substring or
// array of them); otherwise fall back to common keywords. If nothing matches,
// spatial coverage is simply skipped — no behavior depends on it.
const PLAYER_HINTS = (() => {
  const h = controlMap.player ?? controlMap.playerHint;
  if (Array.isArray(h)) return h.map((s) => String(s).toLowerCase()).filter(Boolean);
  if (typeof h === 'string' && h.trim()) return [h.toLowerCase()];
  return ['player', 'hero', 'ship', 'paddle', 'avatar', 'character'];
})();
function findPlayer(fullState) {
  if (!fullState || fullState.error) return null;
  for (const scene of Object.values(fullState.scenes || {})) {
    for (const o of scene.objects || []) {
      if (!o.active) continue;
      const tag = `${o.texture || ''} ${o.name || ''}`.toLowerCase();
      if (PLAYER_HINTS.some((k) => tag.includes(k))) return o;
    }
  }
  return null;
}
function regionCell(x, y) {
  const cx = Math.min(REGIONS_N - 1, Math.max(0, Math.floor((x / DESIGN_W) * REGIONS_N)));
  const cy = Math.min(REGIONS_N - 1, Math.max(0, Math.floor((y / DESIGN_H) * REGIONS_N)));
  return `${cx},${cy}`;
}
// Cheap visual-activity signal (no image decode): how many
// of the captured frames are pixel-distinct (md5) — near 0 => static/inert scene.
function visualActivity(framePaths) {
  const hashes = new Set();
  let counted = 0, totalBytes = 0;
  const sizes = [];
  for (const p of framePaths) {
    try { const buf = readFileSync(p); hashes.add(createHash('md5').update(buf).digest('hex')); counted++; totalBytes += buf.length; sizes.push(buf.length); } catch {}
  }
  const diversity = counted ? hashes.size / counted : 0;
  const mean = sizes.length ? totalBytes / sizes.length : 0;
  const variance = sizes.length ? sizes.reduce((s, v) => s + (v - mean) ** 2, 0) / sizes.length : 0;
  return { frames: counted, distinct: hashes.size, diversity: Math.round(diversity * 100) / 100, size_cv: mean ? Math.round((Math.sqrt(variance) / mean) * 100) / 100 : 0 };
}
function frameSignature(framePath) {
  if (!framePath) return null;
  try { return createHash('md5').update(readFileSync(framePath)).digest('hex'); }
  catch { return null; }
}

// ── Decision: LLM policy ────────────────────────────────────────────────
async function decideLLM(page, objective, memory, recentActions, compact, fullState, framePath, stepNum) {
  const stateText = VISION_ONLY ? '(omitted; rely on the screenshot)' : JSON.stringify(compact);
  const actorsText = VISION_ONLY ? '(omitted; rely on the screenshot)' : summarizeActors(fullState);
  const prompt = `You are an autonomous QA agent playing a 2D browser game to verify it works.
${GAME_CONTEXT ? `GAME CONTEXT: ${GAME_CONTEXT}\n` : ''}
DESIGN RESOLUTION: ${DESIGN_W}x${DESIGN_H} (game-space coordinates; origin top-left).

CURRENT OBJECTIVE: ${objective.operation}
SUCCESS LOOKS LIKE: ${objective.expected_result}

AVAILABLE ACTIONS (choose exactly one each step):
${actionMenu()}

ON-SCREEN OBJECTS (gray-box, exact positions from engine; use these to aim):
${actorsText}

KEY GAME STATE (scores/lives/size etc.): ${stateText}
YOUR SCRATCHPAD MEMORY (persists across steps): ${memory || '(empty)'}
RECENT ACTIONS: ${recentActions.slice(-6).map((a) => a.action + (a.args ? `(${JSON.stringify(a.args)})` : '')).join(' -> ') || '(none)'}

A screenshot of the current frame is also attached. Use the exact object positions above to choose precise coordinates. Decide the single best next action to progress the objective.
Reply with ONLY a JSON object (no prose, no markdown). Keep "thought" and "memory" under 160 characters each:
{ "thought": "...", "action": "<one action name>", "args": { }, "memory": "...", "status": "continue|done|blocked" }
Set status="done" only when the success condition is clearly met. Set status="blocked" if you cannot progress — including when the objective appears IMPOSSIBLE given the rules/state (e.g. no valid target exists); record why in "memory".`;
  const timeout = boundedModelTimeout(DEADLINE);
  if (timeout < 1) return { _error: 'wall-clock decision budget exhausted' };

  const res = await callModel({
    prompt,
    images: [{ path: framePath }],
    model: args.model,
    provider: args.provider,
    maxTokens: Number(args['think-budget']),
    temperature: 0.2,
    timeout,
    label: `decide-${objective.id}-${stepNum}`,
  });
  if (res.error) return { _error: res.error };
  const parsed = extractJson(res.text);
  if (!parsed || !parsed.action) return { _error: `unparseable decision: ${(res.text || '').slice(0, 120)}` };
  return parsed;
}

// ── Decision: heuristic policy (no LLM) — generic exploration ────────────
const heuristicState = { idx: 0 };
function decideHeuristic(objective, compact, recentActions) {
  const names = Object.keys(controlMap.actions);
  // If every active scene looks like a menu/title (a small, predictable
  // vocabulary), try a confirm/advance action. Menus are easier to detect
  // reliably than the open-ended set of gameplay scene names.
  const activeKeys = Object.keys(compact?.scenes || {});
  const looksLikeMenu = activeKeys.length > 0 &&
    activeKeys.every((k) => /title|menu|start|boot|splash|home|select|pause|intro|lobby|loading/i.test(k));
  if (looksLikeMenu) {
    const advance = names.find((n) => controlMap.actions[n].primitive === 'pointer_click')
      || names.find((n) => controlMap.actions[n].primitive === 'key_press');
    if (advance) return { thought: 'menu/title — advance', action: advance, args: {}, status: 'continue' };
  }
  // Otherwise cycle actions; randomize coords for pointer_move.
  const name = names[heuristicState.idx % names.length];
  heuristicState.idx++;
  const spec = controlMap.actions[name];
  const out = { thought: 'heuristic exploration', action: name, args: {}, status: 'continue' };
  if ((spec.argsFromLLM || []).includes('x')) out.args.x = Math.round(Math.random() * DESIGN_W);
  if ((spec.argsFromLLM || []).includes('y')) out.args.y = Math.round(Math.random() * DESIGN_H);
  return out;
}

// ── Programmatic success predicate (optional, strong signal) ─────────────
function getCustom(compact, scene, path) {
  const s = compact?.scenes?.[scene];
  if (!s) return undefined;
  let v = s.custom;
  for (const part of String(path).split('.')) { if (v == null) return undefined; v = v[part]; }
  return v;
}
function cmp(a, op, b) {
  switch (op) { case '>': return a > b; case '>=': return a >= b; case '<': return a < b; case '<=': return a <= b; case '==': return a == b; case '!=': return a != b; default: return false; }
}
function evalPredicate(compact, pred) {
  if (!pred) return null;
  if (pred.activeScene) return Object.keys(compact?.scenes || {}).includes(pred.activeScene);
  if (pred.scene && pred.custom) return cmp(getCustom(compact, pred.scene, pred.custom), pred.op || '>', pred.value ?? 0);
  if (pred.scene && pred.group) {
    const g = compact?.scenes?.[pred.scene]?.groups?.[pred.group];
    if (!g) return false;
    return cmp(g[pred.field || 'active'], pred.op || '>', pred.value ?? 0);
  }
  return null;
}

// ── LLM judge: YES / NO / PARTIAL for a finished objective ───────────────
async function judgeObjective(objective, runSummary, framePath) {
  if (runSummary.predicateMet === true) {
    return { verdict: 'YES', reason: 'success predicate satisfied', bug_suspected: false };
  }
  if (POLICY !== 'llm') {
    // Without an LLM, rely solely on the programmatic predicate result.
    if (runSummary.predicateMet === false) return { verdict: 'NO', reason: 'success predicate not satisfied', bug_suspected: false };
    return { verdict: 'UNVERIFIED', reason: 'no LLM and no predicate', bug_suspected: false };
  }
  const prompt = `You are judging whether a game objective was achieved during automated play.

OBJECTIVE (operation): ${objective.operation}
EXPECTED RESULT: ${objective.expected_result}

EVIDENCE:
- Initial state (gray-box): ${JSON.stringify(runSummary.initialState)}
- Final state (gray-box): ${JSON.stringify(runSummary.finalState)}
- On-screen objects at end (positions + any per-object data fields): ${runSummary.actorsSummary}
- Actions taken: ${runSummary.actions.map((a) => a.action).join(' -> ') || '(none)'}
- Programmatic success predicate: ${runSummary.predicateMet === null ? 'n/a' : runSummary.predicateMet}
- Oracle findings during run: ${runSummary.oracleSummary || 'none'}
- Blocked rationale: ${runSummary.blockedReason || 'none'}
The final screenshot is attached.

Decide if the expected result was achieved. If the objective looks UNACHIEVABLE due to game design/balance (not weak play) — e.g. the agent actively tried but no valid target/path ever existed, or the success state is unreachable — set bug_suspected=true and say so in "bug". Reply with ONLY JSON:
{ "verdict": "YES" | "NO" | "PARTIAL", "reason": "one sentence with concrete evidence", "bug_suspected": true|false, "bug": "short description if bug_suspected else empty" }`;
  const timeout = boundedModelTimeout(HARD_DEADLINE - REPORT_GRACE_MS);
  if (timeout < 1) {
    const bugSuspected = runSummary.status === 'blocked'
      && (
        runSummary.oracleSummary !== 'none'
        || Boolean(runSummary.blockedReason)
      );
    return {
      verdict: 'UNVERIFIED',
      reason: 'judge skipped because the wall-clock report budget was exhausted',
      bug_suspected: bugSuspected,
      bug: bugSuspected
        ? `objective blocked: ${runSummary.blockedReason || runSummary.oracleSummary}`
        : '',
    };
  }
  const res = await callModel({
    prompt, images: framePath ? [{ path: framePath }] : [],
    model: args.model, provider: args.provider, maxTokens: 1024,
    temperature: 0.1, timeout, label: `judge-${objective.id}`,
  });
  if (res.error) {
    if (runSummary.predicateMet === true) return { verdict: 'YES', reason: `judge unavailable (${res.error}); predicate satisfied`, bug_suspected: false };
    const bugSuspected = runSummary.status === 'blocked'
      && (
        runSummary.oracleSummary !== 'none'
        || Boolean(runSummary.blockedReason)
      );
    return {
      verdict: 'UNVERIFIED',
      reason: `judge unavailable: ${res.error}`,
      bug_suspected: bugSuspected,
      bug: bugSuspected
        ? `objective blocked: ${runSummary.blockedReason || runSummary.oracleSummary}`
        : '',
    };
  }
  const parsed = extractJson(res.text);
  if (!parsed || !parsed.verdict) return { verdict: 'UNVERIFIED', reason: 'unparseable judge reply', bug_suspected: false };
  return parsed;
}

// ── Screenshot helper ────────────────────────────────────────────────────
async function snap(page, name) {
  const p = join(FRAMES_DIR, name);
  try { await page.screenshot({ path: p }); return p; } catch { return null; }
}
function nonEmptyFile(path, minBytes = 1500) {
  try { return path && statSync(path).size >= minBytes; } catch { return false; }
}

// ── Run a single objective ──────────────────────────────────────────────
async function runObjective(page, objective, seed) {
  // --max-steps is a hard ceiling; objectives may request fewer.
  const maxSteps = Math.min(objective.max_steps || DEFAULT_MAX_STEPS, DEFAULT_MAX_STEPS);
  const tag = `${objective.id}${SEEDS > 1 ? `_s${seed}` : ''}`;
  log(`\n▶ Objective [${objective.id}] (seed ${seed}): ${objective.operation}`);

  let memory = '';
  let blockedReason = '';
  const recentActions = [];
  const history = [];           // { step, fps, signature }
  const oracleFindings = [];
  let consecutiveFails = 0;     // for the fast-fail "inputs not landing" guard
  let predicateMet = objective.success ? false : null;
  let status = 'continue';
  let stepsTaken = 0;
  // coverage tracking
  const scenesVisited = new Set();
  const actionsUsed = new Set();
  const regionsVisited = new Set();
  const framePaths = [];
  let allSceneKeys = [];
  ACTIVE_OBJECTIVE = {
    id: objective.id,
    seed,
    operation: objective.operation,
    status,
    steps: stepsTaken,
    predicateMet,
    blockedReason,
    memory,
    oracleFindings,
    recentActions,
    finalShot: null,
    phase: 'setup',
  };

  // Warp straight to the state under test (skip replaying from boot).
  if (objective.setup) {
    const su = await applySetup(page, objective.setup);
    log(`  setup: ${su.ok ? `warped → ${objective.setup.scene || 'custom state'}` : `FAILED (${su.error})`}`);
    if (!su.ok) {
      blockedReason = `objective setup failed: ${su.error}`;
      ACTIVE_OBJECTIVE.blockedReason = blockedReason;
    }
  }
  ACTIVE_OBJECTIVE.phase = 'running';

  // initial observation
  await pauseGame(page);
  const initialCompact = await extractStateCompact(page);
  const initialShot = await snap(page, `${tag}_step00_init.png`);
  await resumeGame(page);

  let lastShot = initialShot;
  for (let step = 1; step <= maxSteps; step++) {
    if (Date.now() > DEADLINE) { log('  ! wall-clock budget reached — ending objective early'); break; }
    stepsTaken = step;
    ACTIVE_OBJECTIVE.steps = stepsTaken;

    // 1. pause + observe
    await pauseGame(page);
    const compact = await extractStateCompact(page);
    const full = await extractState(page);
    const consoleErrors = full?.consoleErrors || [];
    const framePath = await snap(page, `${tag}_step${String(step).padStart(2, '0')}.png`);
    lastShot = framePath || lastShot;
    if (framePath) framePaths.push(framePath);

    // coverage: scenes, allSceneKeys, player region
    for (const k of Object.keys(compact?.scenes || {})) scenesVisited.add(k);
    if (full?.allSceneKeys) allSceneKeys = full.allSceneKeys;
    const player = findPlayer(full);
    if (player && Number.isFinite(player.x) && Number.isFinite(player.y)) regionsVisited.add(regionCell(player.x, player.y));

    // 2. oracles (deterministic)
    history.push({
      step,
      fps: compact?.fps,
      signature: stateSignature(compact),
      visualSignature: frameSignature(framePath),
    });
    const findings = runStepOracles({ fullState: full, compactState: compact, consoleErrors, history });
    for (const f of findings) oracleFindings.push({ step, ...f });

    // 3. programmatic success check
    if (objective.success) {
      const met = evalPredicate(compact, objective.success);
      if (met === true) predicateMet = true;
    }
    if (predicateMet === true) {
      status = 'done';
      ACTIVE_OBJECTIVE.status = status;
      ACTIVE_OBJECTIVE.predicateMet = predicateMet;
      log(`  ✓ success predicate already met at observation ${step}; skipping LLM decision`);
      break;
    }
    if (findings.some((finding) => finding.type === 'frozen_state')) {
      status = 'blocked';
      blockedReason = 'state and screenshot remained frozen across the oracle window';
      ACTIVE_OBJECTIVE.status = status;
      ACTIVE_OBJECTIVE.blockedReason = blockedReason;
      log('  ! deterministic frozen-state oracle fired — ending objective before another LLM call');
      break;
    }

    // 4. decide
    let decision;
    if (POLICY === 'llm') {
      decision = await decideLLM(page, objective, memory, recentActions, compact, full, framePath, step);
      if (decision._error) {
        log(`  step ${step}: LLM error (${decision._error}) -> heuristic this step`);
        decision = decideHeuristic(objective, compact, recentActions);
      }
    } else {
      decision = decideHeuristic(objective, compact, recentActions);
    }
    if (decision.memory) memory = decision.memory;
    ACTIVE_OBJECTIVE.memory = memory;
    if (decision.status === 'blocked') {
      blockedReason = decision.memory || decision.thought || 'agent reported blocked';
      ACTIVE_OBJECTIVE.blockedReason = blockedReason;
    }

    // 5. resume + actuate
    await resumeGame(page);
    const t0 = Date.now();
    const actResult = await actuate(page, decision.action, decision.args || {});
    await sleep(ACTION_DELAY);
    const dt = Date.now() - t0;
    for (const f of timingOracle(decision.action, dt)) oracleFindings.push({ step, ...f });

    recentActions.push({ action: decision.action, args: decision.args, ok: actResult.ok });
    if (actResult.ok) actionsUsed.add(decision.action);
    appendFileSync(TRAJECTORY_PATH, JSON.stringify({
      ts: new Date().toISOString(), objective: objective.id, seed, step,
      thought: decision.thought, action: decision.action, args: decision.args || {},
      actuation: actResult, status: decision.status,
      fps: compact?.fps, activeScenes: Object.keys(compact?.scenes || {}),
      predicateMet, findings: findings.map((f) => f.type),
    }) + '\n');

    log(`  step ${step}: ${decision.action}${decision.args && Object.keys(decision.args).length ? `(${JSON.stringify(decision.args)})` : ''} ` +
        `${actResult.ok ? '✓' : '✗ ' + actResult.note} ${findings.length ? `[oracle:${findings.map((f) => f.type).join(',')}]` : ''}`);

    // 5b. fast-fail — never burn the whole step budget on actions that can't land.
    if (!actResult.ok && /^unknown (primitive|action)/i.test(actResult.note || '')) {
      // Structural defect: the interaction-contract maps this action to an unsupported
      // primitive (or the action is undefined), so NO input reaches the game. Retrying
      // is futile and it breaks every objective identically — record loudly + abort all.
      oracleFindings.push({ step, severity: 'crash', oracle: 'actuation', type: 'unactuatable_action',
        message: `action '${decision.action}' cannot be actuated: ${actResult.note}. `
          + `Its interaction-contract primitive is unsupported / the action is undefined, so input never reaches the game `
          + `(e.g. the START button is never really clicked). Fix .playtest/inputs/interaction-contract.json (or the impl).` });
      log(`  ✗ unactuatable '${decision.action}' (${actResult.note}) — aborting (structural; retrying is futile)`);
      status = 'blocked';
      blockedReason = `action '${decision.action}' could not be actuated: ${actResult.note}`;
      ACTIVE_OBJECTIVE.status = status;
      ACTIVE_OBJECTIVE.blockedReason = blockedReason;
      STRUCTURAL_ACTUATION_ERROR = true;
      break;
    }
    if (actResult.ok) consecutiveFails = 0;
    else if (++consecutiveFails >= MAX_CONSECUTIVE_FAILS) {
      oracleFindings.push({ step, severity: 'high', oracle: 'actuation', type: 'inputs_not_landing',
        message: `${consecutiveFails} consecutive actions failed to actuate (last: ${actResult.note || 'no effect'}) — inputs are not reaching the game; ending objective early.` });
      log(`  ! ${consecutiveFails} consecutive failed actuations — ending objective early`);
      status = 'blocked';
      blockedReason = `${consecutiveFails} consecutive actions failed to actuate`;
      ACTIVE_OBJECTIVE.status = status;
      ACTIVE_OBJECTIVE.blockedReason = blockedReason;
      break;
    }

    // 6. termination
    status = decision.status || 'continue';
    ACTIVE_OBJECTIVE.status = status;
    ACTIVE_OBJECTIVE.predicateMet = predicateMet;
    if (status === 'blocked' && !blockedReason) {
      blockedReason = memory || decision.thought || 'agent reported blocked';
      ACTIVE_OBJECTIVE.blockedReason = blockedReason;
    }
    if (predicateMet === true) { log(`  ✓ success predicate met at step ${step}`); break; }
    if (status === 'done') { log(`  ✓ agent reports done at step ${step}`); break; }
    if (status === 'blocked') { log(`  ! agent reports blocked at step ${step}`); break; }
  }

  // final observation + judge
  await pauseGame(page);
  const finalCompact = await extractStateCompact(page);
  const finalFull = await extractState(page);
  const finalShot = await snap(page, `${tag}_final.png`) || lastShot;
  if (objective.success && evalPredicate(finalCompact, objective.success) === true) {
    predicateMet = true;
    status = 'done';
  }
  if (finalShot) framePaths.push(finalShot);
  await resumeGame(page);

  const oracleSummary = oracleFindings.length
    ? [...new Set(oracleFindings.map((f) => `${f.severity}:${f.type}`))].join(', ')
    : 'none';
  const runSummary = {
    initialState: initialCompact, finalState: finalCompact,
    actorsSummary: summarizeActors(finalFull), status,
    actions: recentActions, predicateMet, oracleSummary, blockedReason,
  };
  Object.assign(ACTIVE_OBJECTIVE, {
    status,
    steps: stepsTaken,
    predicateMet,
    blockedReason,
    finalShot,
    phase: 'judging',
  });
  const judgment = await judgeObjective(objective, runSummary, finalShot);

  const visual = visualActivity(framePaths);
  log(`  → verdict: ${judgment.verdict} — ${judgment.reason}`);
  const result = {
    id: objective.id, seed, operation: objective.operation, expected_result: objective.expected_result,
    verdict: judgment.verdict, reason: judgment.reason,
    bug_suspected: !!judgment.bug_suspected, bug: judgment.bug || '',
    steps: stepsTaken, status, predicateMet, blockedReason,
    warped: !!objective.setup,
    oracleFindings, finalShot,
    coverage: {
      scenesVisited: [...scenesVisited], actionsUsed: [...actionsUsed],
      regionsVisited: [...regionsVisited], allSceneKeys,
    },
    visual,
  };
  Object.assign(ACTIVE_OBJECTIVE, {
    ...result,
    phase: 'complete',
  });
  return result;
}

// ── Build-Health gate ────────────────────────────────────────────────────
function computeBuildHealth(booted, results, anyNonEmptyShot) {
  const fatal = results.flatMap((r) => r.oracleFindings).filter((f) => f.severity === 'crash');
  return {
    booted,
    no_fatal_errors: fatal.length === 0,
    produced_nonempty_screenshot: anyNonEmptyShot,
    fatal_errors: fatal.slice(0, 10),
    pass: booted && fatal.length === 0 && anyNonEmptyShot,
  };
}

// ── Main ─────────────────────────────────────────────────────────────────
async function main() {
  mkdirSync(FRAMES_DIR, { recursive: true });
  // fresh trajectory per run
  writeFileSync(TRAJECTORY_PATH, '');

  // Hoisted so a hard watchdog can preserve browser-verified epoch identity
  // in its partial report instead of misclassifying a timeout as missing
  // runtime identity.
  let runtimeIdentity = null;
  let booted = false;
  const allResults = [];
  const startedAt = Date.now();
  HARD_DEADLINE = startedAt + MAX_WALL_MS;
  const softReserve = Math.min(
    70000,
    Math.max(REPORT_GRACE_MS, Math.floor(MAX_WALL_MS * 0.3)),
  );
  DEADLINE = HARD_DEADLINE - softReserve;

  // Watchdog: guarantee the process can never hang indefinitely (e.g. a stalled
  // navigation or a wedged browser). Write a partial report before exiting so the
  // MCP handler can surface trajectory context instead of bare MISSING_ARTIFACT.
  const watchdog = setTimeout(() => {
    log(`\nWATCHDOG: exceeded ${MAX_WALL_MS / 1000}s wall-clock cap — writing partial report and forcing exit.`);
    try {
      const preserved = [...allResults];
      if (
        ACTIVE_OBJECTIVE
        && !preserved.some(
          (result) => result.id === ACTIVE_OBJECTIVE.id
            && result.seed === ACTIVE_OBJECTIVE.seed,
        )
      ) {
        preserved.push(ACTIVE_OBJECTIVE);
      }
      const blockedRationale = (result) => (
        result.blockedReason || result.blocked_reason || result.memory || ''
      );
      const objectiveDetails = preserved.map((result) => ({
        id: result.id,
        seed: result.seed,
        verdict: result.verdict || 'UNVERIFIED',
        reason: result.reason || `watchdog interrupted objective during ${result.phase || 'execution'}`,
        steps: result.steps || 0,
        status: result.status || 'blocked',
        blocked_reason: blockedRationale(result),
        predicate_met: result.predicateMet ?? null,
        bug_suspected: !!result.bug_suspected,
        bug: result.bug || '',
        final_screenshot: result.finalShot || null,
      }));
      const preservedBugs = preserved.flatMap((result) => [
        ...(result.bug_suspected
          ? [{ source: 'judge', objective: result.id, message: result.bug || result.reason }]
          : []),
        ...(result.status === 'blocked' && blockedRationale(result)
          ? [{
            source: 'play-agent',
            objective: result.id,
            type: 'blocked_objective',
            message: blockedRationale(result),
          }]
          : []),
        ...((result.oracleFindings || []).map((finding) => ({
          source: finding.oracle,
          objective: result.id,
          step: finding.step,
          severity: finding.severity,
          type: finding.type,
          message: finding.message,
        }))),
      ]);
      const scored = preserved.filter(
        (result) => ['YES', 'NO', 'PARTIAL'].includes(result.verdict),
      );
      const scoreWeight = (verdict) => (
        verdict === 'YES' ? 1 : verdict === 'PARTIAL' ? 0.5 : 0
      );
      const anyShot = preserved.some((result) => nonEmptyFile(result.finalShot));
      const watchdogFinding = {
        severity: 'crash',
        oracle: 'watchdog',
        type: 'wall_clock_exceeded',
        message: `Watchdog fired at ${MAX_WALL_MS / 1000}s — process was still running (likely stuck in an LLM call or an unresponsive game loop).`,
      };
      const partial = {
        schema_version: 2,
        source: 'play-agent.mjs',
        run_id: RUN_ID,
        generated: new Date().toISOString(),
        game: GAME_DIR,
        url: args.url,
        runtime_identity: runtimeIdentity,
        watchdog_timeout: true,
        build_health: {
          booted,
          no_fatal_errors: false,
          produced_nonempty_screenshot: anyShot,
          pass: false,
          fatal_errors: [watchdogFinding],
        },
        weighted_accuracy: scored.length
          ? scored.reduce((sum, result) => sum + scoreWeight(result.verdict), 0) / scored.length
          : null,
        objective_scores: [],
        objectives_detail: objectiveDetails,
        bugs: [
          ...preservedBugs,
          {
            source: 'watchdog',
            type: 'wall_clock_exceeded',
            message: `Process exceeded ${MAX_WALL_MS / 1000}s wall-clock cap. Check trajectory.jsonl for the last action attempted.`,
          },
        ],
        trajectory: TRAJECTORY_PATH,
      };
      writeFileSync(REPORT_PATH, JSON.stringify(partial, null, 2));
    } catch {}
    process.exit(4);
  }, MAX_WALL_MS);
  watchdog.unref();

  const browser = await chromium.launch({ headless: !args.headed });
  // Viewport follows the game's design size (control-map canvas) so screenshots
  // aren't clipped for games that aren't the default 800x600.
  const contextOpts = { viewport: { width: DESIGN_W, height: DESIGN_H } };
  if (args.record) {
    const recDir = join(PLAY_DIR, 'recordings');
    mkdirSync(recDir, { recursive: true });
    contextOpts.recordVideo = { dir: recDir, size: { width: DESIGN_W, height: DESIGN_H } };
  }
  const context = await browser.newContext(contextOpts);
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(NAV_TIMEOUT);
  await installErrorCapture(page);

  log(`Opening ${args.url} ...`);
  // domcontentloaded (not networkidle): CDN/proxy can keep the network "busy"
  // forever, and we only need the DOM + the Phaser boot signal below.
  await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT }).catch((e) => log(`  goto warning: ${e.message}`));
  runtimeIdentity = await verifyRuntimeIdentity(page, args);
  try { await waitForGame(page, 15000); booted = true; }
  catch { log('  ! game did not boot (window.__phaserGame not ready)'); }

  let budgetHit = false;
  if (booted) {
    for (let seed = 1; seed <= SEEDS && !budgetHit; seed++) {
      for (const objective of OBJECTIVES) {
        if (Date.now() > DEADLINE) { log('Skipping remaining objectives (wall-clock budget).'); budgetHit = true; break; }
        // reset to a clean game between objectives by reloading the page
        if (allResults.length > 0) {
          await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT }).catch(() => {});
          await verifyRuntimeIdentity(page, args);
          try { await waitForGame(page, 15000); } catch {}
          await sleep(500);
        }
        const r = await runObjective(page, objective, seed);
        allResults.push(r);
        ACTIVE_OBJECTIVE = null;
        if (STRUCTURAL_ACTUATION_ERROR) {
          log('Structural actuation error (unsupported primitive / undefined action) — skipping remaining objectives; the same broken contract affects them all. Fix the interaction-contract, then re-run.');
          budgetHit = true;
          break;
        }
      }
    }
  }
  // Objectives may execute arbitrary authored interactions; prove they did not
  // navigate the browser away from the epoch runtime before sealing evidence.
  await verifyRuntimeIdentity(page, args);

  // Aggregate (weighted accuracy; average across seeds per objective)
  const weight = (v) => (v === 'YES' ? 1 : v === 'PARTIAL' ? 0.5 : 0);
  const byObjective = {};
  for (const r of allResults) {
    (byObjective[r.id] ||= []).push(r);
  }
  const objectiveScores = Object.entries(byObjective).map(([id, runs]) => {
    const score = runs.reduce((s, r) => s + weight(r.verdict), 0) / runs.length;
    return { id, operation: runs[0].operation, verdicts: runs.map((r) => r.verdict), score };
  });
  const verified = allResults.filter((r) => r.verdict !== 'UNVERIFIED');
  const weightedAccuracy = verified.length
    ? verified.reduce((s, r) => s + weight(r.verdict), 0) / verified.length
    : null;

  // collect frames for non-empty check
  let anyNonEmptyShot = false;
  for (const r of allResults) { if (nonEmptyFile(r.finalShot)) { anyNonEmptyShot = true; break; } }

  const buildHealth = computeBuildHealth(booted, allResults, anyNonEmptyShot);

  const allBugs = allResults.flatMap((r) => [
    ...(r.bug_suspected ? [{ source: 'judge', objective: r.id, message: r.bug || r.reason }] : []),
    ...r.oracleFindings.filter((f) => f.severity !== 'warn' || f.type === 'entity_leak' || f.type === 'frozen_state')
      .map((f) => ({ source: f.oracle, objective: r.id, step: f.step, severity: f.severity, type: f.type, message: f.message, evidence: f.evidence })),
  ]);
  // ── Coverage aggregation (TITAN-style) ──
  const allActions = Object.keys(controlMap.actions);
  const scenesSeen = new Set(), actionsSeen = new Set(), regionsSeen = new Set();
  let allScenes = [];
  for (const r of allResults) {
    (r.coverage?.scenesVisited || []).forEach((s) => scenesSeen.add(s));
    (r.coverage?.actionsUsed || []).forEach((a) => actionsSeen.add(a));
    (r.coverage?.regionsVisited || []).forEach((c) => regionsSeen.add(c));
    if (r.coverage?.allSceneKeys?.length) allScenes = r.coverage.allSceneKeys;
  }
  const sceneTotal = allScenes.length || scenesSeen.size || 1;
  const coverage = {
    scenes_visited: [...scenesSeen], scenes_total: sceneTotal,
    scene_pct: Math.round((scenesSeen.size / sceneTotal) * 100) / 100,
    actions_used: [...actionsSeen], actions_total: allActions.length,
    action_pct: allActions.length ? Math.round((actionsSeen.size / allActions.length) * 100) / 100 : null,
    regions_visited: regionsSeen.size, regions_total: REGIONS_N * REGIONS_N,
    region_pct: Math.round((regionsSeen.size / (REGIONS_N * REGIONS_N)) * 100) / 100,
  };

  // ── Visual-activity bug: a scene that never visibly changed across many frames ──
  for (const r of allResults) {
    if (r.visual && r.visual.frames >= 4 && r.visual.diversity <= 0.25) {
      allBugs.push({ source: 'visual', objective: r.id, type: 'inert_scene',
        message: `little/no visual change during '${r.id}' (${r.visual.distinct}/${r.visual.frames} distinct frames) — possible frozen render or missing animation` });
    }
  }

  // de-dup bug messages
  const seenBug = new Set();
  const bugs = allBugs.filter((b) => { const k = `${b.type || ''}:${b.message}`; if (seenBug.has(k)) return false; seenBug.add(k); return true; });

  // ── Regression detection vs previous run (history append-only) ──
  const HISTORY_PATH = join(PLAY_DIR, 'agent-play-history.jsonl');
  const curVerdicts = Object.fromEntries(objectiveScores.map((o) => [o.id, o.score]));
  let regressions = [];
  try {
    if (existsSync(HISTORY_PATH)) {
      const lines = readFileSync(HISTORY_PATH, 'utf-8').trim().split('\n').filter(Boolean);
      if (lines.length) {
        const prev = JSON.parse(lines[lines.length - 1]);
        for (const [id, score] of Object.entries(curVerdicts)) {
          const prevScore = prev.scores?.[id];
          if (typeof prevScore === 'number' && score < prevScore) {
            regressions.push({ id, previous: prevScore, current: score });
          }
        }
      }
    }
  } catch {}

  const report = {
    schema_version: 2,
    source: 'play-agent.mjs',
    run_id: RUN_ID,
    generated: new Date().toISOString(),
    game: GAME_DIR,
    url: args.url,
    runtime_identity: runtimeIdentity,
    policy: POLICY,
    provider: POLICY === 'llm' ? detectProvider({ provider: args.provider, model: args.model }) : null,
    vision_only: VISION_ONLY,
    pause_while_thinking: !REALTIME,
    seeds: SEEDS,
    build_health: buildHealth,
    weighted_accuracy: weightedAccuracy,
    coverage,
    regressions,
    changed_files: CHANGED_FILES.length ? CHANGED_FILES : undefined,
    objective_scores: objectiveScores,
    objectives_detail: allResults.map((r) => ({
      id: r.id, seed: r.seed, verdict: r.verdict, reason: r.reason, steps: r.steps,
      status: r.status, blocked_reason: r.blockedReason,
      predicate_met: r.predicateMet, warped: r.warped,
      bug_suspected: r.bug_suspected, bug: r.bug,
      visual: r.visual, final_screenshot: r.finalShot,
    })),
    bugs,
    oracle_finding_count: allResults.reduce((n, r) => n + r.oracleFindings.length, 0),
    trajectory: TRAJECTORY_PATH,
  };
  writeFileSync(REPORT_PATH, JSON.stringify(report, null, 2));

  // append to history for cross-run regression tracking
  try {
    appendFileSync(HISTORY_PATH, JSON.stringify({
      schema_version: 2, source: 'play-agent.mjs', run_id: RUN_ID,
      ts: report.generated, policy: POLICY, scores: curVerdicts,
      weighted_accuracy: weightedAccuracy, build_health: buildHealth.pass,
      coverage: { scene_pct: coverage.scene_pct, action_pct: coverage.action_pct, region_pct: coverage.region_pct },
      runtime_identity: runtimeIdentity,
    }) + '\n');
  } catch {}

  // finalize recording
  if (args.record) { try { await page.close(); await context.close(); } catch {} }
  try { await browser.close(); } catch {}

  // ── Summary to stderr ──
  log('\n────────── Agent Play Report ──────────');
  log(`Build health: ${buildHealth.pass ? 'PASS' : 'FAIL'} (booted=${buildHealth.booted}, fatal_errors=${!buildHealth.no_fatal_errors}, screenshot=${buildHealth.produced_nonempty_screenshot})`);
  log(`Weighted accuracy: ${weightedAccuracy === null ? 'n/a (no LLM judge/predicate)' : (weightedAccuracy * 100).toFixed(0) + '%'}`);
  for (const o of objectiveScores) log(`  [${o.id}] ${(o.score * 100).toFixed(0)}%  ${o.verdicts.join(',')}`);
  log(`Coverage: scenes ${(coverage.scene_pct * 100).toFixed(0)}% (${coverage.scenes_visited.length}/${coverage.scenes_total}), ` +
      `actions ${coverage.action_pct === null ? 'n/a' : (coverage.action_pct * 100).toFixed(0) + '%'} (${coverage.actions_used.length}/${coverage.actions_total}), ` +
      `regions ${(coverage.region_pct * 100).toFixed(0)}%`);
  if (regressions.length) { log(`Regressions vs last run: ${regressions.length}`); for (const rg of regressions) log(`  - ${rg.id}: ${rg.previous} -> ${rg.current}`); }
  log(`Bugs found: ${bugs.length}`);
  for (const b of bugs.slice(0, 8)) log(`  - (${b.source}) ${b.message}`);
  log(`Report: ${REPORT_PATH}`);
  log(`Trajectory: ${TRAJECTORY_PATH}`);

  // exit code: non-zero if build health fails (lets gate-check/CI react)
  process.exit(buildHealth.pass ? 0 : 1);
}

main().catch((e) => { log('FATAL:', e.stack || e.message); process.exit(3); });
