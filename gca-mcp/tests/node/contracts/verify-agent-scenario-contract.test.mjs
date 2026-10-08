import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const verifyAgent = readFileSync(
  join(repoRoot, '.claude', 'agents', 'verify-agent.md'),
  'utf8',
);
const dispatchContract = readFileSync(
  join(
    repoRoot,
    '.claude',
    'skills',
    'game-pipeline',
    'dispatch-contract.md',
  ),
  'utf8',
);
const pipelineSkill = readFileSync(
  join(repoRoot, '.claude', 'skills', 'game-pipeline', 'SKILL.md'),
  'utf8',
);
const implementAgent = readFileSync(
  join(repoRoot, '.claude', 'agents', 'implement-agent.md'),
  'utf8',
);
const integrationAgent = readFileSync(
  join(repoRoot, '.claude', 'agents', 'asset-integration-agent.md'),
  'utf8',
);
const iterationProcedures = readFileSync(
  join(
    repoRoot,
    '.claude',
    'skills',
    'game-pipeline',
    'references',
    'iteration-procedures.md',
  ),
  'utf8',
);
const playAgentReference = readFileSync(
  join(
    repoRoot,
    '.claude',
    'skills',
    'game-pipeline',
    'references',
    'verify',
    'play-agent.md',
  ),
  'utf8',
);
const testPatternsReference = readFileSync(
  join(
    repoRoot,
    '.claude',
    'skills',
    'game-pipeline',
    'references',
    'verify',
    'test-patterns.md',
  ),
  'utf8',
);

function frontmatterTools(document) {
  return document.match(/^tools:\s*(.+)$/m)?.[1] || '';
}

test('scenario verification shares one epoch and serializes only recordings', () => {
  assert.match(
    verifyAgent,
    /Do not dispatch one verify-agent per scenario/,
  );
  assert.match(
    verifyAgent,
    /every `iterate`\/`full` pass owns exactly one `run_verify_dag` call, which owns\s+exactly one verify epoch and one aggregate report/,
  );
  assert.match(verifyAgent, /records each scenario sequentially and alone/);
  assert.match(
    verifyAgent,
    /runs all analyses and core browser producers in its bounded parallel batch/,
  );
  assert.match(verifyAgent, /one `mcp__gca__run_verify_dag` call/);
  assert.doesNotMatch(dispatchContract, /`SCENARIO`/);
});

test('interrupted verification explicitly resumes the matching active epoch', () => {
  assert.match(verifyAgent, /`resume_active:true`/);
  assert.match(dispatchContract, /`resume_active:true`/);
  assert.match(pipelineSkill, /Active epoch after an interrupted transport/);
  assert.match(pipelineSkill, /resume the same physical verify-agent\/task/);
  assert.match(pipelineSkill, /exact\s+active `verify_run_id`, mode, and URL/);
  assert.match(dispatchContract, /Starting a fresh verifier\/epoch is forbidden/);
});

test('only verify agent executes epoch-bound browser tests', () => {
  assert.doesNotMatch(frontmatterTools(implementAgent), /mcp__gca__run_tests/);
  assert.doesNotMatch(frontmatterTools(integrationAgent), /mcp__gca__run_tests/);
  assert.match(frontmatterTools(verifyAgent), /mcp__gca__run_tests/);
  assert.match(integrationAgent, /execution": "deferred_to_verify"/);
  assert.match(
    iterationProcedures,
    /visual_diff\(\{ game_dir: GAME_DIR, url: RUN\.url, verify_run_id: RUN\.verify_run_id/,
  );
});

test('visual review receipt is refreshed for each verify epoch', () => {
  assert.match(pipelineSkill, /persist `visual_verified:false`/);
  assert.match(pipelineSkill, /visual_verified: true/);
  assert.match(
    verifyAgent,
    /Screenshot review receipt \| `visual_verified` \| `state\.json\.visual_verified`/,
  );
});

test('physics applicability matches the gate routing contract', () => {
  assert.match(
    verifyAgent,
    /interaction_profile:"none"` produces explicit\s+`applicable:false, aligned:null`/,
  );
  assert.match(
    verifyAgent,
    /`arcade-gravity`, `arcade-no-gravity`, and\s+`matter` are required/,
  );
  assert.match(
    verifyAgent,
    /materialize_physics_alignment/,
  );
  assert.match(
    verifyAgent,
    /do not hand-author\s+or overwrite `alignment\/physics\/alignment\.json`/,
  );
  assert.match(
    verifyAgent,
    /measured\s+body-to-art fit, world bounds, hit-target size, tunneling risk/,
  );
  assert.doesNotMatch(
    verifyAgent,
    /Turn the engine's collision debug render ON/,
  );
});

test('layout failures preserve issues when no artifact was written', () => {
  assert.match(
    verifyAgent,
    /`materialize_layout_alignment` writes[\s\S]{0,30}layout from current bounds/,
  );
  assert.match(
    verifyAgent,
    /An analysis failure becomes a failed checkpoint and a partial\/blocked report/,
  );
  assert.match(
    verifyAgent,
    /failed\/running checkpoints into\s+structured issues/,
  );
  assert.match(
    verifyAgent,
    /finally.*calls `write_verify_report`/s,
  );
});

test('video analysis references use the pinned model policy', () => {
  assert.match(playAgentReference, /pinned-Doubao video\s+analysis/);
});

test('noncanonical test directories remain scoped diagnostics', () => {
  assert.match(
    testPatternsReference,
    /`tests` path is not the exact canonical `.playtest\/tests\/` directory/,
  );
});
