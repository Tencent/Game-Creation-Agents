import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const read = (...parts) => readFileSync(join(repoRoot, ...parts), 'utf8');

const levelDesignAgent = read('.claude', 'agents', 'level-design-agent.md');
const scaffoldAgent = read('.claude', 'agents', 'scaffold-agent.md');
const implementAgent = read('.claude', 'agents', 'implement-agent.md');
const pipeline = read('.claude', 'skills', 'game-pipeline', 'SKILL.md');
const dispatch = read(
  '.claude',
  'skills',
  'game-pipeline',
  'dispatch-contract.md',
);

test('authored level inventory is dynamic, complete, and forwarded exactly', () => {
  assert.match(levelDesignAgent, /level_files:/);
  assert.match(levelDesignAgent, /complete ordered inventory/);
  assert.match(levelDesignAgent, /never derive filenames from\s+`stage_count`/);
  assert.match(dispatch, /`LEVEL_FILES`.*Complete ordered JSON array/);
  assert.match(pipeline, /Bind the validated list to `LEVEL_FILES`/);
  assert.match(
    pipeline,
    /pass it unchanged\s+to every authored Scaffold and\s+Implement dispatch/i,
  );
  assert.match(pipeline, /state\.level_files/);
  assert.match(pipeline, /resume\.level_files/);
  assert.doesNotMatch(pipeline, /details\.level_files/);
});

test('downstream authored agents read supplied files without rediscovery', () => {
  for (const agent of [scaffoldAgent, implementAgent]) {
    assert.match(agent, /every path in `LEVEL_FILES`/);
    assert.match(agent, /do not use Glob to rediscover stages/);
    assert.doesNotMatch(agent, /`levels\/\*\.json`/);
  }
  assert.match(
    implementAgent,
    /must not copy, embed, or\s+re-express authored stage payloads outside `LEVEL_FILES`/,
  );
  assert.doesNotMatch(implementAgent, /do not create `level-data\.js`/);
  assert.match(
    implementAgent,
    /Never serialize\s+their absolute paths into browser code/,
  );
  assert.match(implementAgent, /game-relative `levels\/<basename>` URLs/);
});

test('authored revisions refresh inventory before implementation and re-gate Step 3', () => {
  assert.match(pipeline, /After every authored `level-design-agent MODE=revise` return/);
  assert.match(pipeline, /rebind the result as `LEVEL_FILES` before dispatching/);
  assert.match(
    pipeline,
    /gate_check\(\{gate:"step_done", step:3\}\)/,
  );
});
