#!/usr/bin/env node
/**
 * gate-check.mjs — Pipeline gate enforcement (CLI entry / thin shim).
 *
 * Checks that a pipeline step's requirements are actually met before allowing
 * the agent to proceed. Returns exit code 1 with specific BLOCKED messages if
 * any check fails, 0 on pass, 2 on an unknown gate.
 *
 * The implementation is modular: run state lives in gate-context.mjs, the
 * gate* checks in gate-engine.mjs, the name -> checks dispatch in
 * gate-registry.mjs, and the dependency-free helpers under shared/. This entry
 * only wires the dispatch to the human report + structured JSON payload so the
 * CLI contract (flags, exit codes, stderr BLOCKED lines) is unchanged.
 *
 * Usage:
 *   node gate-check.mjs --game playground/croc-bath --gate first_pass_complete
 *   node gate-check.mjs --game playground/croc-bath --gate ready_to_ship
 *   node gate-check.mjs --game playground/croc-bath --gate step_done --step 3
 *   node gate-check.mjs --game playground/chrono-rift-td --gate td_contract
 */

import { args, failures, blockingIssues } from './gate-context.mjs';
import { runGate } from './gate-registry.mjs';
import { metadataFor } from './gate-metadata.mjs';

runGate();

// ── Output ─────────────────────────────────────────────────────────────
const gate = args.gate;
const blocked = failures.length > 0;

// P3: machine-readable payload -> stdout (one JSON line). The JSON-emitting
// gates (build_digest / gate_contract / materialize_layout_alignment) already
// printed their own payload and exited inside runGate, so this only fires for
// the deterministic pass/fail gates.
//
// The structured failures mirror the stderr BLOCKED lines 1:1. `failures` is the
// authoritative list — it includes failures produced OUTSIDE runChecks (the
// gateStateJson pre-check in composite gates, and the direct single/composite
// gate calls used by the diagnostic tools), which the previous
// "blockingIssues-or-nothing" form could drop. runChecks records per-check
// routing into `blockingIssues`; we index it by message and enrich each failure,
// falling back to the invoked gate's own metadata so every issue still carries
// check/axis/agent.
const attributed = new Map();
for (const issue of blockingIssues) {
  if (!attributed.has(issue.message)) attributed.set(issue.message, issue);
}
const gateMeta = metadataFor(gate);
const issues = failures.map((f) => {
  const message = f.replace(/^BLOCKED:\s*/, '');
  return attributed.get(message) || { check: gate, ...gateMeta, message };
});
console.log(JSON.stringify({ gate, blocked, failures: issues }));

// Human-readable report + exit code -> stderr. The `BLOCKED: ...` lines are the
// backward-compatible shim consumed by existing tests + agent prose.
if (blocked) {
  console.error(`\n❌ Gate "${gate}" FAILED — ${failures.length} issue(s):\n`);
  for (const f of failures) {
    console.error(`  ${f}`);
  }
  console.error('');
  process.exit(1);
} else {
  console.error(`✅ Gate "${gate}" PASSED`);
  process.exit(0);
}
