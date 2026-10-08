/**
 * gate-context.mjs — Shared run context for the split gate modules.
 *
 * The single owner of the process-wide gate run state that every gate function
 * used to close over as module-level globals in the gate-check.mjs monolith:
 * the parsed CLI args, the resolved game / playtest / config paths, and the
 * accumulated `failures` list.
 *
 * These are exported as ES-module bindings computed once at import time
 * (process.argv is available then). Because gate-check.mjs is a short-lived
 * subprocess (one gate per `node gate-check.mjs ...` invocation, spawned fresh
 * by the MCP layer and the test suite), a module singleton is exactly one run —
 * there is no cross-run contamination. Gate modules import these bindings and
 * read them at call time; `fail()` mutates the shared `failures` array.
 */

import { existsSync } from 'fs';
import { join, resolve } from 'path';
import { parseArgs } from 'node:util';

export const { values: args } = parseArgs({
  options: {
    game: { type: 'string' },
    gate: { type: 'string' },
    step: { type: 'string' },
    config: { type: 'string' },
    'min-accuracy': { type: 'string' }, // play_quality threshold override
  },
});

export const gameDir = resolve(args.game);
export const playtestDir = join(gameDir, '.playtest');
export const configPath = resolve(
  args.config || process.env.GCA_CONFIG_PATH || join(gameDir, '..', '..', 'gca.config.yaml'),
);

// Accumulated blocking messages (each prefixed `BLOCKED: `). Shared, mutable,
// single-run. gate-check.mjs reads `failures` after dispatch to decide exit code
// and to emit the human-readable report + the structured JSON payload.
export const failures = [];

// Structured mirror of `failures`, one record per blocking message with its
// originating check + routing metadata (axis / agent). Populated by the
// registry's runChecks (per-check attribution); the shim falls back to
// attributing any remaining failures to the invoked gate name. This is the P3
// machine-readable channel — the stderr BLOCKED lines remain the compat shim.
export const blockingIssues = [];

export function fail(msg) { failures.push(`BLOCKED: ${msg}`); }
export function fileExists(p) { return existsSync(p); }
