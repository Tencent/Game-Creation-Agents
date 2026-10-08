import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

const packageRoot = fileURLToPath(new URL('../../../', import.meta.url));
const playAgentPath = join(
  packageRoot,
  'src',
  'gca_mcp',
  'runners',
  'verify',
  'play-agent.mjs',
);
const source = readFileSync(playAgentPath, 'utf8');
const oracles = await import(pathToFileURL(join(
  packageRoot,
  'src',
  'gca_mcp',
  'runners',
  'verify',
  'oracles.mjs',
)));

test('play watchdog is bounded and preserves verified runtime identity', () => {
  assert.match(
    source,
    /'max-wall':\s*\{\s*type:\s*'string',\s*default:\s*'240'/,
  );
  const identityDeclaration = source.indexOf('let runtimeIdentity = null');
  const watchdog = source.indexOf('const watchdog = setTimeout');
  const identitySeal = source.indexOf('runtime_identity: runtimeIdentity', watchdog);
  const identityVerification = source.indexOf(
    'runtimeIdentity = await verifyRuntimeIdentity',
  );
  assert.ok(identityDeclaration >= 0 && identityDeclaration < watchdog);
  assert.ok(identitySeal > watchdog && identitySeal < identityVerification);
  assert.match(source.slice(watchdog, identityVerification), /objectives_detail: objectiveDetails/);
  assert.match(source.slice(watchdog, identityVerification), /\.\.\.preservedBugs/);
  assert.match(source.slice(watchdog, identityVerification), /blocked_reason: blockedRationale/);
  assert.match(source, /boundedModelTimeout\(HARD_DEADLINE - REPORT_GRACE_MS\)/);
});

test('deterministic success and frozen-state checks precede an LLM decision', () => {
  const runObjective = source.slice(
    source.indexOf('async function runObjective'),
    source.indexOf('// ── Build-Health gate'),
  );
  const predicateFastPath = runObjective.indexOf('if (predicateMet === true)');
  const frozenFastPath = runObjective.indexOf(
    "findings.some((finding) => finding.type === 'frozen_state')",
  );
  const llmDecision = runObjective.indexOf(
    'decision = await decideLLM',
  );
  assert.ok(predicateFastPath >= 0 && predicateFastPath < llmDecision);
  assert.ok(frozenFastPath >= 0 && frozenFastPath < llmDecision);
  assert.doesNotMatch(runObjective, /MAX_STUCK_STEPS/);
});

test('machine predicates bypass the final LLM judge', () => {
  const judge = source.slice(
    source.indexOf('async function judgeObjective'),
    source.indexOf('// ── Screenshot helper'),
  );
  assert.ok(
    judge.indexOf('runSummary.predicateMet === true')
      < judge.indexOf("if (POLICY !== 'llm')"),
  );
  assert.doesNotMatch(
    judge,
    /runSummary\.status === 'blocked'\s*&&\s*runSummary\.predicateMet/,
  );
  assert.match(judge, /objective blocked:/);
  const runObjective = source.slice(
    source.indexOf('async function runObjective'),
    source.indexOf('// ── Build-Health gate'),
  );
  assert.match(
    runObjective,
    /evalPredicate\(finalCompact, objective\.success\) === true/,
  );
});

test('blocked rationale survives setup watchdogs and final reports', () => {
  const runObjective = source.slice(
    source.indexOf('async function runObjective'),
    source.indexOf('// ── Build-Health gate'),
  );
  assert.ok(
    runObjective.indexOf('ACTIVE_OBJECTIVE = {')
      < runObjective.indexOf('await applySetup'),
  );
  assert.match(runObjective, /blockedReason/);
  assert.match(source, /blocked_reason: r\.blockedReason/);
  assert.match(source, /Blocked rationale:/);
});

test('frozen-state oracle requires unchanged pixels as well as state', () => {
  const moving = Array.from({ length: 6 }, (_, index) => ({
    step: index + 1,
    signature: 'same-compact-state',
    visualSignature: `frame-${index}`,
  }));
  assert.deepEqual(oracles.stuckOracle(moving), []);

  const frozen = moving.map((sample) => ({
    ...sample,
    visualSignature: 'same-frame',
  }));
  assert.equal(oracles.stuckOracle(frozen)[0].type, 'frozen_state');
});
