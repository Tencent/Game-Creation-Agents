import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const implementRoot = join(
  repoRoot,
  '.claude/skills/game-pipeline/references/implement',
);

function readReference(name) {
  return readFileSync(join(implementRoot, name), 'utf8');
}

function codeFences(reference) {
  return [...reference.matchAll(/```(?:js|javascript)\r?\n([\s\S]*?)\r?\n```/g)]
    .map((match) => match[1]);
}

function assertExampleParses(block, label) {
  const importLines = block.split('\n').filter((line) => /^\s*import\s/.test(line));
  const body = block.split('\n').filter((line) => !/^\s*import\s/.test(line)).join('\n').trim();

  if (!body) {
    assert.ok(importLines.length > 0, `${label} must contain code beyond imports`);
    return;
  }

  try {
    Function(body);
    return;
  } catch (functionError) {
    try {
      Function(`return class ReferenceExample {\n${body}\n}`);
      return;
    } catch (methodError) {
      assert.fail(
        `${label} is neither valid module-style code nor a valid class method:\n`
        + `${functionError.message}\n${methodError.message}`,
      );
    }
  }
}

function countUpdateMethods(reference) {
  const blocks = codeFences(reference);
  let count = 0;
  for (const block of blocks) {
    const matches = block.match(/\bupdate\s*\(\s*time\s*,\s*delta\s*\)\s*\{/g) || [];
    count += matches.length;
  }
  return count;
}

test('card and idle references provide distinct genre runtime loops', () => {
  const card = readReference('card.md');
  const idle = readReference('idle.md');

  assert.notEqual(card, idle);
  assert.match(card, /^# Card Implementation Patterns/m);
  assert.match(idle, /^# Idle Implementation Patterns/m);
  assert.match(card, /^> Genre: `card`/m);
  assert.match(idle, /^> Genre: `idle`/m);

  for (const reference of [card, idle]) {
    assert.doesNotMatch(reference, /^## [0-9]+\./m);
    assert.doesNotMatch(reference, /Pipeline routing family|External taxonomy/);
    assert.doesNotMatch(reference, /^## Focused tests$/m);
  }

  for (const term of [
    'deck',
    'hand',
    'discard',
    'board',
    'effectQueue',
    'instantiateDeck',
    'playCard',
    'resolveNextCardEffect',
  ]) {
    assert.match(card, new RegExp(term), `card.md must cover ${term}`);
  }

  for (const term of [
    'productionPerSecond',
    'advanceEconomy',
    'maxOfflineMs',
    'saveVersion',
    'loadIdleState',
    'saveIdleState',
    'buyGenerator',
    'prestige',
    'meetsGeneratorPrerequisites',
  ]) {
    assert.match(idle, new RegExp(term), `idle.md must cover ${term}`);
  }

  for (const staleUiPattern of [
    /## Button Helper/,
    /## Card Hand Display/,
    /## Dialogue System/,
    /renderHand\(/,
    /createButton\(/,
  ]) {
    assert.doesNotMatch(card, staleUiPattern);
    assert.doesNotMatch(idle, staleUiPattern);
  }
});

test('card and idle references keep declared UI source strategies', () => {
  const card = readReference('card.md');
  const idle = readReference('idle.md');

  for (const reference of [card, idle]) {
    assert.match(reference, /placeUiText/);
    assert.match(reference, /bindUiSlots/);
    assert.match(reference, /declared source strategies/);
    assert.doesNotMatch(reference, /fontSize:\s*['"][0-9]+px/);
    assert.doesNotMatch(reference, /backgroundColor:/);
  }
});

test('card and idle JavaScript examples remain parseable', () => {
  for (const name of ['card.md', 'idle.md']) {
    const blocks = codeFences(readReference(name));
    assert.ok(blocks.length > 0, `${name} must contain JavaScript examples`);
    blocks.forEach((block, index) => {
      assertExampleParses(block, `${name} JavaScript block ${index + 1}`);
    });
  }
});

test('bullet hell and roguelike keep distinct loops with one Scene update', () => {
  const bulletHell = readReference('bullet_hell.md');
  const roguelike = readReference('roguelike.md');

  assert.notEqual(bulletHell, roguelike);
  assert.doesNotMatch(bulletHell, /top-down-controller\.md/);
  assert.doesNotMatch(roguelike, /top-down-controller\.md/);
  assert.equal(countUpdateMethods(bulletHell), 1);
  assert.equal(countUpdateMethods(roguelike), 1);

  for (const reference of [bulletHell, roguelike]) {
    assert.match(reference, /applyTopDownMovement/);
    assert.match(reference, /aimAtPointer/);
  }

  for (const term of [
    'playerHurtbox',
    'maxSize',
    'registerInteractions',
    'tagEntityInteractionLayer',
    'emitRadialPattern',
    'maxPatternsPerFrame',
    'recycleEnemyBullets',
  ]) {
    assert.match(bulletHell, new RegExp(term), `bullet_hell.md must cover ${term}`);
  }

  for (const term of [
    'RandomDataGenerator',
    'runState',
    'rngState',
    'rollDrop',
    'addRelic',
    'metaState',
    'finishRun',
  ]) {
    assert.match(roguelike, new RegExp(term), `roguelike.md must cover ${term}`);
  }

  assert.match(roguelike, /rngState: this\.runRng\.state\(\)/);

  for (const staleHeading of [
    /^## 8-Direction Movement/m,
    /^## Aim Toward Mouse/m,
    /^## Enemy Patrol AI/m,
  ]) {
    assert.doesNotMatch(bulletHell, staleHeading);
    assert.doesNotMatch(roguelike, staleHeading);
  }
});

test('bullet hell and roguelike JavaScript examples remain parseable', () => {
  for (const name of ['bullet_hell.md', 'roguelike.md']) {
    const blocks = codeFences(readReference(name));
    assert.ok(blocks.length > 0, `${name} must contain JavaScript examples`);
    blocks.forEach((block, index) => {
      assertExampleParses(block, `${name} JavaScript block ${index + 1}`);
    });
  }
});

test('puzzle reference provides an action, settle, and terminal loop', () => {
  const puzzle = readReference('puzzle.md');

  assert.match(puzzle, /^# Puzzle Implementation Patterns/m);
  assert.match(puzzle, /^> Genre: `puzzle`/m);
  assert.doesNotMatch(puzzle, /^## [0-9]+\./m);
  assert.match(puzzle, /attempt\.moveDelta/);

  for (const term of [
    'Sokoban',
    '2048',
    'Match-3',
    'Tetris-like',
    'Minesweeper-like',
    'applyPuzzleAction',
    'resolveUntilStable',
    'puzzleRules.evaluate',
    'finishPuzzle',
  ]) {
    assert.match(puzzle, new RegExp(term), `puzzle.md must cover ${term}`);
  }
});

test('simulation reference provides transactions, settlement, and milestones', () => {
  const simulation = readReference('simulation.md');

  assert.match(simulation, /^# Simulation Implementation Patterns/m);
  assert.match(simulation, /^> Genre: `simulation`/m);
  assert.doesNotMatch(simulation, /^## [0-9]+\./m);
  assert.doesNotMatch(simulation, /this\.applyRandomEvents\(\)/);
  assert.match(simulation, /seed: scenario\.seed/);
  assert.match(simulation, /Number\.isFinite\(current\)/);

  for (const term of [
    'queueManagementCommand',
    'applyManagementCommand',
    'settleProduction',
    'simTick',
    'applyAuthoredTickEvent',
    'evaluateMilestones',
    'finishSimulation',
  ]) {
    assert.match(simulation, new RegExp(term), `simulation.md must cover ${term}`);
  }
});

test('tower defence reference provides adapter, wave loop, and combat loop', () => {
  const towerDefence = readReference('tower_defence.md');

  assert.match(towerDefence, /^# Tower Defence Implementation Patterns/m);
  assert.match(towerDefence, /^> Genre: `tower_defence`/m);
  assert.match(towerDefence, /instance methods of one `Phaser\.Scene`/);
  assert.match(towerDefence, /normalizeTowerDefenceScenario/);
  assert.match(towerDefence, /validateTowerDefenceScenario/);
  assert.match(towerDefence, /unlocked_at_wave/);
  assert.match(towerDefence, /overrides/);
  assert.doesNotMatch(towerDefence, /^## [0-9]+\./m);
  assert.doesNotMatch(towerDefence, /this\.scene/);
  assert.doesNotMatch(towerDefence, /TerrainAssembler/);
  assert.match(towerDefence, /this\.onBuildSlotClicked/);
  assert.equal(countUpdateMethods(towerDefence), 1);

  for (const term of [
    'startNextWave',
    'updateWave',
    'spawnEnemy',
    'leakEnemy',
    'placeTower',
    'upgradeTower',
    'sellTower',
    'selectTowerTarget',
    'updateTowers',
    'applyDamage',
    'killEnemy',
    'completeWaveIfClear',
    'finishGame',
  ]) {
    assert.match(towerDefence, new RegExp(term), `tower_defence.md must cover ${term}`);
  }
});

test('puzzle, simulation, and tower defence JavaScript examples remain parseable', () => {
  for (const name of ['puzzle.md', 'simulation.md', 'tower_defence.md']) {
    const blocks = codeFences(readReference(name));
    assert.ok(blocks.length > 0, `${name} must contain JavaScript examples`);
    blocks.forEach((block, index) => {
      assertExampleParses(block, `${name} JavaScript block ${index + 1}`);
    });
  }
});

test('genre references use structured camera routing and dispatched interaction profile', () => {
  const beatEmUp = readReference('beat_em_up.md');
  const platform = readReference('platform.md');
  const custom = readReference('custom.md');

  for (const reference of [beatEmUp, platform, custom]) {
    assert.doesNotMatch(reference, /camera:\s*(?:side-scroll|top-down|auto-scroll)/);
  }

  assert.match(beatEmUp, /camera\.view: side-on/);
  assert.match(beatEmUp, /camera\.view: top-down/);
  assert.match(beatEmUp, /camera\.behavior/);
  assert.match(beatEmUp, /INTERACTION_PROFILE=arcade-gravity/);
  assert.match(beatEmUp, /INTERACTION_PROFILE=arcade-no-gravity/);

  assert.match(platform, /camera\.view: side-on/);
  assert.match(platform, /camera\.behavior: auto-scroll/);
  assert.match(platform, /INTERACTION_PROFILE=arcade-gravity/);

  assert.match(custom, /Implement-agent \*\*is dispatched\*\* the required/);
  assert.match(custom, /consume it directly and never infer a replacement/);
  assert.match(custom, /require exact agreement between/);
  assert.match(custom, /camera\.view/);
  assert.match(custom, /camera\.behavior/);
  assert.match(custom, /For Arcade profiles only, use `registerInteractions/);
  assert.match(custom, /import `CharacterController`/);
  assert.doesNotMatch(custom, /not dispatched\s+`?interaction_profile/);
  assert.doesNotMatch(custom, /derive the engineering profile/);
  assert.doesNotMatch(custom, /copy `character-controller\.js`/);
});

test('particle atom initialization belongs only to Implement entry Scene', () => {
  const fxGuide = readReference('fx-guide.md');
  const particleRecipes = readReference('particle_recipes.js');
  const implementAgent = readFileSync(
    join(repoRoot, '.claude/agents/implement-agent.md'),
    'utf8',
  );
  const scaffoldAgent = readFileSync(
    join(repoRoot, '.claude/agents/scaffold-agent.md'),
    'utf8',
  );

  assert.match(fxGuide, /actual entry Scene declared by GDD §1/);
  assert.match(fxGuide, /Scaffold only\s+copies the helper/);
  assert.match(fxGuide, /Keep every Scene stub behavior-free/);
  assert.match(particleRecipes, /actual GDD entry Scene's create\(\)/);
  assert.match(particleRecipes, /Scaffold only copies this helper/);
  assert.match(implementAgent, /Call `buildAtoms` once in the actual entry Scene/);
  assert.match(scaffoldAgent, /Keep scene stubs parseable but behavior-free/);

  assert.doesNotMatch(fxGuide, /In `BootScene\.create\(\)`, add/);
  assert.doesNotMatch(particleRecipes, /Call ONCE from BootScene\.create\(\)/);
  assert.doesNotMatch(
    particleRecipes,
    /@deprecated Use `buildAtoms` from BootScene\.create\(\)/,
  );

  assert.match(
    fxGuide,
    /import \{ buildAtoms, recipes \} from '\.\.\/utils\/particle-recipes\.js';/,
  );
  assert.match(fxGuide, /create\(\) \{\s*\n\s*buildAtoms\(this\);/);
});

test('melee references point to the canonical display helper owner', () => {
  const beatEmUp = readReference('beat_em_up.md');
  const roguelike = readReference('roguelike.md');
  const custom = readReference('custom.md');
  const displayUtils = readReference('runtime/display-utils.js');

  assert.match(
    displayUtils,
    /export function hitboxOverlapsHurtbox\(/,
  );
  assert.match(
    beatEmUp,
    /import \{ applyDisplay, hitboxOverlapsHurtbox \} from '\.\.\/display-utils\.js';/,
  );
  assert.match(beatEmUp, /source of truth is `runtime\/display-utils\.js`/);
  assert.match(beatEmUp, /CONFIG\.attacks\[attackName\]/);
  assert.match(beatEmUp, /resolveEntityKey/);
  assert.match(custom, /canonical `hitboxOverlapsHurtbox` helper/);

  for (const reference of [beatEmUp, roguelike, custom]) {
    assert.doesNotMatch(reference, /implement-agent's three-box model/);
    assert.doesNotMatch(reference, /implement-agent §6\.8/);
  }

  const beatBlocks = codeFences(beatEmUp);
  assert.ok(beatBlocks.length > 0, 'beat_em_up.md must contain JavaScript examples');
  beatBlocks.forEach((block, index) => {
    assertExampleParses(block, `beat_em_up.md JavaScript block ${index + 1}`);
  });
});

test('card reference avoids undispatched chance routing', () => {
  const card = readReference('card.md');
  assert.doesNotMatch(card, /DESIGN_AXES\.chance/);
  assert.match(card, /CONFIG\.chance|cardRules\.chanceModel/);
});

test('idle online production is not capped by offline limit', () => {
  const idle = readReference('idle.md');
  assert.match(idle, /this\.economyAccumulatorMs \+= elapsedMs;/);
  assert.doesNotMatch(idle, /economyAccumulatorMs \+= Math\.min\(\s*elapsedMs, this\.idleRules\.maxOfflineMs/);
});
