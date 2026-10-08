import assert from 'node:assert/strict';
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import {
  assertObjectivesDocument,
  validateObjectivesDocument,
} from '../../../src/gca_mcp/runners/verify/objectives-contract.mjs';

const playAgentPath = fileURLToPath(
  new URL('../../../src/gca_mcp/runners/verify/play-agent.mjs', import.meta.url),
);

function validDocument() {
  return {
    schema_version: 1,
    objectives: [
      {
        id: 'start_game',
        operation: 'Start the game.',
        expected_result: 'GameScene becomes active.',
        max_steps: 2,
        success: { activeScene: 'GameScene' },
      },
      {
        id: 'score',
        operation: 'Score once.',
        expected_result: 'The score increases.',
        scenes: ['GameScene'],
        setup: {
          scene: 'GameScene',
          stop: ['TitleScene'],
          data: { level: 1 },
          state: { GameScene: { score: 0 } },
        },
        success: {
          scene: 'GameScene',
          custom: 'score',
          op: '>',
          value: 0,
        },
      },
      {
        id: 'clear_targets',
        operation: 'Clear the targets.',
        expected_result: 'No active targets remain.',
        success: {
          scene: 'GameScene',
          group: 'targets',
          field: 'active',
          op: '==',
          value: 0,
        },
      },
    ],
  };
}

test('runtime objective contract accepts only executable setup and success forms', () => {
  const document = validDocument();
  assert.deepEqual(validateObjectivesDocument(document), []);
  assert.equal(assertObjectivesDocument(document), document);
});

test('runtime objective contract fails fast on legacy descriptive keys', () => {
  const document = validDocument();
  document.objectives[1].setup = {
    seedBoard: 'place two adjacent matching tiles',
  };
  document.objectives[1].success = {
    read: ['score'],
    predicate: 'score increased',
  };

  const errors = validateObjectivesDocument(document);
  assert.ok(errors.some((error) => error.includes('.setup.seedBoard: unknown field')));
  assert.ok(errors.some((error) => error.includes('choose exactly one predicate form')));
  assert.throws(
    () => assertObjectivesDocument(document, '/tmp/objectives.json'),
    /Invalid objectives contract.*seedBoard/s,
  );
});

test('runtime objective contract rejects mixed predicates and missing start_game', () => {
  const document = validDocument();
  document.objectives[1].success = {
    activeScene: 'GameScene',
    state: { score: 0 },
  };
  document.objectives.shift();

  const errors = validateObjectivesDocument(document);
  assert.ok(errors.some((error) => error.includes('.success.state: unknown field')));
  assert.ok(errors.some((error) => error.includes('must include the start_game')));
});

test('runtime objective contract requires scene for scene-start arguments', () => {
  const document = validDocument();
  document.objectives[1].setup = {
    stop: ['TitleScene'],
    data: { level: 2 },
  };

  const errors = validateObjectivesDocument(document);
  assert.ok(errors.some((error) => error.includes('.setup.scene: required')));
});

test('runtime objective contract rejects duplicate ids and whitespace strings', () => {
  const document = validDocument();
  document.objectives[1].id = 'start_game';
  document.objectives[1].operation = '   ';
  document.objectives[1].setup = { state: { '\t': {} } };

  const errors = validateObjectivesDocument(document);
  assert.ok(errors.some((error) => error.includes('duplicate objective id')));
  assert.ok(errors.some((error) => error.includes('.operation: expected non-empty')));
  assert.ok(errors.some((error) => error.includes('scene key must be non-empty')));
});

test('play agent fails fast when objectives contract is missing', () => {
  const missingGame = join(tmpdir(), `gca-missing-objectives-${process.pid}-${Date.now()}`);
  const result = spawnSync(
    process.execPath,
    [playAgentPath, '--game', missingGame, '--out', join(missingGame, 'out')],
    { encoding: 'utf8', timeout: 5000 },
  );

  assert.equal(result.status, 2, result.stderr);
  assert.match(result.stderr, /objectives contract not found/);
  assert.doesNotMatch(result.stderr, /browser|provider/i);
});

test('play agent fails fast on invalid DSL before provider selection', (t) => {
  const game = mkdtempSync(join(tmpdir(), 'gca-invalid-objectives-'));
  t.after(() => rmSync(game, { recursive: true, force: true }));
  const inputs = join(game, '.playtest', 'inputs');
  mkdirSync(inputs, { recursive: true });
  const document = validDocument();
  document.objectives[1].setup = { seedBoard: [[2, 2]] };
  writeFileSync(
    join(inputs, 'objectives.json'),
    JSON.stringify(document),
    'utf8',
  );

  const result = spawnSync(
    process.execPath,
    [playAgentPath, '--game', game, '--out', join(game, 'out')],
    { encoding: 'utf8', timeout: 5000 },
  );

  assert.equal(result.status, 2, result.stderr);
  assert.match(result.stderr, /seedBoard: unknown field/);
  assert.doesNotMatch(result.stderr, /Opening |Selected provider|chromium\.launch/i);
});
