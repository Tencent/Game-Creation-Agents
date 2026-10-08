#!/usr/bin/env node
/**
 * oracles.mjs — deterministic bug oracles for the play-agent.
 *
 * Each oracle inspects observation state (from state-extractor) and/or the
 * rolling step history and returns zero or more structured findings:
 *   { oracle, severity, type, message, evidence }
 *
 * These run every step during agent play. The LLM "logic oracle" lives in
 * play-agent.mjs (it needs model access); these are cheap, objective, and
 * reproducible — the kind of signals screenshots/video miss.
 *
 * Severity: 'crash' > 'error' > 'warn'.
 */

const DEFAULTS = {
  entityLeakCount: 600,     // total objects above this => suspected leak
  groupLeakCount: 400,      // single group above this => suspected leak
  fpsFloor: 12,             // sustained fps below this => perf problem
  fpsFloorStreak: 3,        // consecutive low-fps samples to flag
  stuckSteps: 6,            // identical signature for this many steps => stuck
};

/** Crash / uncaught-error oracle. Reads consoleErrors captured on the page. */
export function crashOracle(consoleErrors = []) {
  const findings = [];
  const seen = new Set();
  for (const e of consoleErrors) {
    const msg = String(e);
    // De-dup identical messages so one recurring error isn't reported N times.
    const keyHash = msg.slice(0, 160);
    if (seen.has(keyHash)) continue;
    seen.add(keyHash);
    const fatal = /\[uncaught\]|\[unhandled rejection\]|is not a function|cannot read|undefined is not|TypeError|ReferenceError/i.test(msg);
    findings.push({
      oracle: 'crash',
      severity: fatal ? 'crash' : 'error',
      type: fatal ? 'runtime_exception' : 'console_error',
      message: msg.slice(0, 300),
      evidence: { source: 'console' },
    });
  }
  return findings;
}

/** Invariant oracle: NaN/Infinity positions & velocities, entity leaks, fps floor. */
export function invariantOracle(fullState, history = [], cfg = {}) {
  const c = { ...DEFAULTS, ...cfg };
  const findings = [];
  if (!fullState || fullState.error) return findings;

  // NaN / Infinity positions and velocities
  for (const [sceneKey, scene] of Object.entries(fullState.scenes || {})) {
    for (const obj of scene.objects || []) {
      if (!obj.active) continue;
      if (!Number.isFinite(obj.x) || !Number.isFinite(obj.y)) {
        findings.push({ oracle: 'invariant', severity: 'error', type: 'nan_position',
          message: `${obj.type}${obj.texture ? `(${obj.texture})` : ''} at non-finite position in ${sceneKey}`,
          evidence: { scene: sceneKey, x: obj.x, y: obj.y, texture: obj.texture } });
      }
      if (obj.body && (!Number.isFinite(obj.body.velocity?.x) || !Number.isFinite(obj.body.velocity?.y))) {
        findings.push({ oracle: 'invariant', severity: 'error', type: 'nan_velocity',
          message: `${obj.type} has non-finite velocity in ${sceneKey}`,
          evidence: { scene: sceneKey, velocity: obj.body.velocity } });
      }
    }
    // Group leaks
    for (const [g, info] of Object.entries(scene.groups || {})) {
      if ((info.total ?? 0) > c.groupLeakCount) {
        findings.push({ oracle: 'invariant', severity: 'warn', type: 'entity_leak',
          message: `group '${g}' has ${info.total} members (> ${c.groupLeakCount}) in ${sceneKey}`,
          evidence: { scene: sceneKey, group: g, total: info.total } });
      }
    }
  }

  // Total object count leak (use history to confirm sustained growth)
  if ((fullState.totalObjectCount ?? 0) > c.entityLeakCount) {
    findings.push({ oracle: 'invariant', severity: 'warn', type: 'entity_leak',
      message: `total object count ${fullState.totalObjectCount} (> ${c.entityLeakCount})`,
      evidence: { totalObjectCount: fullState.totalObjectCount } });
  }

  // FPS floor over a streak
  const fpsSamples = [...history.slice(-(c.fpsFloorStreak - 1)).map((h) => h.fps), fullState.fps]
    .filter((f) => typeof f === 'number' && f > 0);
  if (fpsSamples.length >= c.fpsFloorStreak && fpsSamples.every((f) => f < c.fpsFloor)) {
    findings.push({ oracle: 'invariant', severity: 'warn', type: 'low_fps',
      message: `fps below ${c.fpsFloor} for ${fpsSamples.length} samples (${fpsSamples.join(',')})`,
      evidence: { fpsSamples } });
  }

  return findings;
}

/**
 * Compute a compact, comparable signature of a state snapshot.
 * Used by the stuck oracle: if the signature is identical across many steps,
 * the game is likely frozen/hung (or the agent is in a no-op loop).
 */
export function stateSignature(compactState) {
  if (!compactState || compactState.error) return 'err';
  const parts = [];
  for (const [k, s] of Object.entries(compactState.scenes || {})) {
    const groups = Object.entries(s.groups || {}).map(([g, v]) => `${g}:${v.active}/${v.total}`).join(',');
    const custom = Object.entries(s.custom || {})
      .filter(([, v]) => ['number', 'string', 'boolean'].includes(typeof v))
      .map(([p, v]) => `${p}=${v}`).join(',');
    parts.push(`${k}|${groups}|${custom}`);
  }
  return `${compactState.objectCount}#${parts.join(';')}`;
}

/** Stuck/hang oracle: identical signature for >= stuckSteps consecutive steps. */
export function stuckOracle(history = [], cfg = {}) {
  const c = { ...DEFAULTS, ...cfg };
  if (history.length < c.stuckSteps) return [];
  const recent = history.slice(-c.stuckSteps);
  const sig = recent[0].signature;
  const visualSig = recent[0].visualSignature;
  if (
    sig
    && sig !== 'err'
    && visualSig
    && recent.every(
      (h) => h.signature === sig && h.visualSignature === visualSig,
    )
  ) {
    return [{ oracle: 'stuck', severity: 'warn', type: 'frozen_state',
      message: `state and screenshot signatures unchanged for ${c.stuckSteps} steps (possible hang or no-op loop)`,
      evidence: { signature: sig, visualSignature: visualSig, steps: recent.map((h) => h.step) } }];
  }
  return [];
}

/** Timing oracle: an action that took far longer than expected to actuate. */
export function timingOracle(action, durationMs, expectedMs = 1500) {
  if (durationMs > expectedMs * 4) {
    return [{ oracle: 'timing', severity: 'warn', type: 'slow_action',
      message: `action '${action}' took ${durationMs}ms (expected ~${expectedMs}ms)`,
      evidence: { action, durationMs } }];
  }
  return [];
}

/** Run all deterministic oracles for one step and return merged findings. */
export function runStepOracles({ fullState, compactState, consoleErrors, history, cfg }) {
  return [
    ...crashOracle(consoleErrors),
    ...invariantOracle(fullState, history, cfg),
    ...stuckOracle(history, cfg),
  ];
}
