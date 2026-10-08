# Tower Defence Implementation Patterns

> Genre: `tower_defence` (fixed-overhead defence). The dominant loop is start an authored wave → spawn enemies on declared routes → acquire targets and fire → resolve kills/leaks and rewards → buy, upgrade, or sell towers → reach victory or defeat.

> `LEVEL_MODEL=authored` consumes routes, build slots, core, spawns, and waves from the Level DSL/map layout. Board/procedural models consume the same topology contract from their declared authority. Directory existence never selects routing, and authored waypoints are never synthesized or interpolated.

## Level-Model Adapter

For `LEVEL_MODEL=authored`, the shared level loader returns the Level DSL stage
plus resolved `map_layout`. The Scene-owned adapter below normalizes that data
into the runtime `scenario` shape used by the examples in this file. Do not pass
raw `levels/stage_*.json` or `map-layout.json` fields directly into gameplay
without this normalization step.

The adapter must:

- map `starter_kit.money` / `starter_kit.lives` → `startMoney` / `startLives`;
- assign stable build-slot ids (`slot_1`, `slot_2`, …) because schema slots only
  carry geometry; read `textureKey` and `depth` from `CONFIG.td.ui`;
- map `paths[].unlocked_at_wave` → `unlockedAtWave`, global `road_width` → each
  route's `roadWidth`, and route `roadColor` / `depth` from `CONFIG.td`;
- group flat `spawns[]` by `wave`, convert `at` / `interval` seconds to
  milliseconds relative to that wave's first spawn, and preserve `hp`,
  `behavior`, and `params` overrides on each queue entry;
- read `clearReward` from `CONFIG.waves.clearRewards[waveNumber]` (CONFIG-owned,
  not Level DSL);
- validate every spawn `path`, enemy `type`, build slot, and route unlock before
  `create()` commits gameplay state.

```javascript
normalizeTowerDefenceScenario(stage, mapLayout) {
  const waveStarts = new Map();
  for (const spawn of stage.spawns) {
    const current = waveStarts.get(spawn.wave);
    waveStarts.set(spawn.wave, current === undefined ? spawn.at : Math.min(current, spawn.at));
  }

  const wavesByNumber = new Map();
  for (const spawn of stage.spawns) {
    const waveStart = waveStarts.get(spawn.wave) ?? 0;
    const groups = wavesByNumber.get(spawn.wave) || [];
    groups.push({
      enemyType: spawn.type,
      routeId: spawn.path,
      count: spawn.count ?? 1,
      startMs: Math.max(0, (spawn.at - waveStart) * 1000),
      intervalMs: Math.max(0, (spawn.interval ?? 0) * 1000),
      overrides: {
        hp: spawn.hp,
        behavior: spawn.behavior,
        params: spawn.params
      }
    });
    wavesByNumber.set(spawn.wave, groups);
  }

  const waves = [...wavesByNumber.entries()]
    .sort(([a], [b]) => a - b)
    .map(([waveNumber, groups]) => ({
      waveNumber,
      groups,
      clearReward: CONFIG.waves.clearRewards?.[waveNumber] ?? 0
    }));

  const buildSlots = mapLayout.build_slots.map((slot, index) => ({
    id: `slot_${index + 1}`,
    x: slot.x,
    y: slot.y,
    width: slot.width ?? 64,
    height: slot.height ?? 64,
    onPath: slot.on_path === true,
    textureKey: CONFIG.td.ui.buildSlotKey,
    depth: CONFIG.td.ui.buildSlotDepth
  }));

  const paths = mapLayout.paths.map(path => ({
    id: path.id,
    spawn: path.spawn,
    waypoints: path.waypoints,
    unlockedAtWave: path.unlocked_at_wave ?? 1,
    roadWidth: mapLayout.road_width ?? 48,
    roadColor: CONFIG.td.roadColor,
    depth: CONFIG.td.roadDepth
  }));

  return {
    startMoney: stage.starter_kit?.money ?? CONFIG.td.startMoney,
    startLives: stage.starter_kit?.lives ?? CONFIG.td.startLives,
    waves,
    topology: {
      canvas: mapLayout.canvas,
      core: mapLayout.core,
      spawns: mapLayout.spawns,
      paths,
      buildSlots
    }
  };
}

validateTowerDefenceScenario(scenario) {
  const routeIds = new Set(scenario.topology.paths.map(path => path.id));
  for (const wave of scenario.waves) {
    for (const group of wave.groups) {
      if (!routeIds.has(group.routeId)) {
        throw new Error(`Unknown route: ${group.routeId}`);
      }
      if (!CONFIG.enemies[group.enemyType]) {
        throw new Error(`Unknown enemy type: ${group.enemyType}`);
      }
      const route = scenario.topology.paths.find(path => path.id === group.routeId);
      if (route.unlockedAtWave > wave.waveNumber) {
        throw new Error(`Route ${group.routeId} is not unlocked for wave ${wave.waveNumber}`);
      }
    }
  }
  if (scenario.topology.buildSlots.length === 0) {
    throw new Error('Tower defence requires at least one build slot');
  }
}
```

## Scene State + Topology

All methods in this reference are instance methods of one `Phaser.Scene`. The
level-model adapter returns plain scenario/topology data only; it does not create
or retain GameObjects. The Scene directly creates roads, props, slots, enemies,
and towers, stores their runtime collections, and owns their input callbacks.

The gameplay model owns money, lives, wave progress, and terminal status. Phaser groups/sprites are views with runtime combat fields.

```javascript
create() {
  const { stage, mapLayout } = this.loadTowerDefenceStage(); // declared level-model loader
  const scenario = this.normalizeTowerDefenceScenario(stage, mapLayout);
  this.validateTowerDefenceScenario(scenario);

  this.tdState = {
    money: scenario.startMoney,
    lives: scenario.startLives,
    waveIndex: -1,
    status: 'ready'
  };
  this.waveSchedule = scenario.waves;
  this.spawnQueue = [];
  this.paths = {};
  this.routeDefs = scenario.topology.paths;
  this.enemies = this.add.group();
  this.towers = this.add.group();

  this.createBuildSlots(scenario.topology.buildSlots);
  this.placeCoreAndSpawns(scenario.topology);
  this.renderTowerDefenceState(this.tdState);

  if (CONFIG.waves.autoStart) this.startNextWave(this.time.now);
}

createBuildSlots(slotDefs) {
  this.buildSlots = new Map();
  for (const def of slotDefs) {
    const view = this.add.image(def.x, def.y, def.textureKey)
      .setDepth(def.depth).setInteractive({ useHandCursor: true });
    const slot = { ...def, view, tower: null };
    view.on('pointerdown', () => this.onBuildSlotClicked(def.id));
    this.buildSlots.set(def.id, slot);
  }
}
```

`placeCoreAndSpawns` creates sprites at the exact topology coordinates. Build slots, spawns, and the defended core are interactive props—not details painted into the map base.

## One Route Object for Drawing + Movement

Build each unlocked route once. The visible road and every enemy walking that route consume the same `Phaser.Curves.Path`, so the art and movement cannot drift.

```javascript
buildRoute(routeDef) {
  if (routeDef.waypoints.length < 2) {
    throw new Error(`Route ${routeDef.id} needs at least two authored waypoints`);
  }
  const [start, ...rest] = routeDef.waypoints;
  const path = new Phaser.Curves.Path(start.x, start.y);
  for (const point of rest) path.lineTo(point.x, point.y);
  return path;
}

activateRoutesForWave(waveNumber) {
  for (const routeDef of this.routeDefs) {
    if (routeDef.unlockedAtWave > waveNumber || this.paths[routeDef.id]) continue;
    const path = this.buildRoute(routeDef);
    this.paths[routeDef.id] = path;

    const road = this.add.graphics().setDepth(routeDef.depth);
    road.lineStyle(routeDef.roadWidth, routeDef.roadColor, 1);
    path.draw(road); // draw the exact object used by updateEnemies
  }
}
```

Do not create a second smoothed path for visuals, guess midpoints between spawn/core, or unlock a route merely because a file exists.

## Wave Schedule

Each normalized wave contains groups such as
`{ enemyType, routeId, count, startMs, intervalMs, overrides }`. Expand them into
a deterministic time/id queue. Validation happens in
`validateTowerDefenceScenario` before gameplay begins.

```javascript
startNextWave(time) {
  if (this.tdState.status !== 'ready') return false;
  if (this.spawnQueue.length || this.enemies.getChildren().length) return false;

  const waveIndex = this.tdState.waveIndex + 1;
  const wave = this.waveSchedule[waveIndex];
  if (!wave) {
    this.finishGame(true);
    return false;
  }

  this.activateRoutesForWave(wave.waveNumber);
  this.waveStartedAt = time;
  this.spawnQueue = this.expandWave(wave).sort((a, b) =>
    a.spawnAtMs - b.spawnAtMs || a.order - b.order
  );
  this.tdState = { ...this.tdState, waveIndex, status: 'running' };
  this.renderTowerDefenceState(this.tdState);
  return true;
}

expandWave(wave) {
  const queue = [];
  let order = 0;
  for (const group of wave.groups) {
    for (let index = 0; index < group.count; index += 1) {
      queue.push({
        enemyType: group.enemyType,
        routeId: group.routeId,
        spawnAtMs: group.startMs + index * group.intervalMs,
        order: order++,
        overrides: group.overrides || {}
      });
    }
  }
  return queue;
}

updateWave(time) {
  if (this.tdState.status !== 'running') return;
  let spawned = 0;
  while (this.spawnQueue.length
    && time - this.waveStartedAt >= this.spawnQueue[0].spawnAtMs
    && spawned < CONFIG.waves.maxSpawnsPerFrame) {
    this.spawnEnemy(this.spawnQueue.shift());
    spawned += 1;
  }
}
```

`maxSpawnsPerFrame` is an authored positive guard that prevents a resumed tab from freezing while catching up. It does not discard overdue enemies.

## Enemy Movement + Core Leaks

```javascript
spawnEnemy(entry) {
  const path = this.paths[entry.routeId];
  const def = CONFIG.enemies[entry.enemyType];
  if (!path || !def) throw new Error('Wave references an unknown route or enemy');

  const start = path.getStartPoint();
  const enemy = this.add.sprite(start.x, start.y, def.textureKey).setDepth(def.depth);
  if (def.walkAnimationKey) enemy.play(def.walkAnimationKey, true);
  const hp = entry.overrides?.hp ?? def.health;
  Object.assign(enemy, {
    alive: true,
    enemyType: entry.enemyType,
    routeId: entry.routeId,
    pathT: 0,
    hp,
    maxHp: hp,
    speed: def.speed,
    reward: def.reward,
    leakDamage: def.leakDamage,
    behavior: entry.overrides?.behavior ?? def.behavior,
    params: entry.overrides?.params ?? null
  });
  this.enemies.add(enemy);
}

updateEnemies(delta) {
  for (const enemy of [...this.enemies.getChildren()]) {
    if (!enemy.alive) continue;
    const path = this.paths[enemy.routeId];
    enemy.pathT += (enemy.speed / path.getLength()) * (delta / 1000);

    if (enemy.pathT >= 1) {
      this.leakEnemy(enemy);
      if (this.tdState.status === 'complete') return;
      continue;
    }
    const point = path.getPoint(enemy.pathT);
    enemy.setPosition(point.x, point.y);
  }
}

leakEnemy(enemy) {
  if (!enemy.alive) return;
  enemy.alive = false;
  enemy.destroy();
  const lives = Math.max(0, this.tdState.lives - enemy.leakDamage);
  this.tdState = { ...this.tdState, lives };
  if (lives === 0) this.finishGame(false);
}
```

A leaked enemy never grants its kill reward. Guard `alive` so simultaneous attacks/leaks cannot resolve one enemy twice.

## Placement, Upgrade + Sale Transactions

Tower definitions own asset keys, targeting mode, build cost, level stats, upgrade costs, and sell-refund rules. Validate the full operation before spending money or changing a slot.

**Beside the road vs. on the lane.** The default TD shape puts towers *beside* a
route, and `gateMapLayoutPresent` enforces it: a build slot must clear every
path by `road_width/2 + max(width,height)/2 + build_slot_margin_px`. Lane-defence
games (plants on a lawn row) place defenders *on* the route instead, which can
never satisfy that clearance. Such a slot sets `on_path: true` in the map
layout, and the gate inverts the requirement rather than dropping it — the
footprint must then *overlap* the road, so the flag cannot be used to push an
off-road slot past the check. Both branches share one threshold, and the switch
`gates.tower_defence.map_layout.allow_on_path_build_slots` can forbid on-path
slots entirely.

Two implementation consequences. An on-path slot sits in the enemy walking
corridor, so its tower needs a collision or blocking rule of its own (enemies
eat it, or route around it) rather than the beside-the-road assumption that
towers and enemies never occupy the same space. And its depth must sort against
moving enemies, not just against the terrain.

```javascript
placeTower(slotId, type) {
  const slot = this.buildSlots.get(slotId);
  const def = CONFIG.towers[type];
  if (!slot || slot.tower || !def || this.tdState.money < def.buildCost) return false;

  const tower = this.add.sprite(slot.x, slot.y, def.textureKey).setDepth(def.depth);
  Object.assign(tower, {
    type,
    level: 0,
    slotId,
    targeting: def.targeting,
    lastFiredAt: -Infinity
  });
  slot.tower = tower;
  this.towers.add(tower);
  this.tdState = { ...this.tdState, money: this.tdState.money - def.buildCost };
  this.renderTowerDefenceState(this.tdState);
  return true;
}

upgradeTower(tower) {
  if (!tower?.active) return false;
  const def = CONFIG.towers[tower.type];
  if (!def) return false;
  const nextLevel = def.levels?.[tower.level + 1];
  const upgradeCost = nextLevel?.upgradeCost;
  if (!nextLevel || !Number.isFinite(upgradeCost) || upgradeCost < 0) return false;
  if (this.tdState.money < upgradeCost) return false;

  this.tdState = {
    ...this.tdState,
    money: this.tdState.money - upgradeCost
  };
  tower.level += 1;
  this.refreshTowerView(tower);
  this.renderTowerDefenceState(this.tdState);
  return true;
}

sellTower(tower) {
  if (!tower?.active) return false;
  const refund = this.tdRules.getSellRefund(tower.type, tower.level);
  const slot = this.buildSlots.get(tower.slotId);
  if (slot) slot.tower = null;
  tower.destroy();
  this.tdState = { ...this.tdState, money: this.tdState.money + refund };
  this.renderTowerDefenceState(this.tdState);
  return true;
}
```

## Targeting, Fire + Kill Rewards

Target selection is deterministic and authored (`first`, `last`, `strong`, and so on). Filter invalid/out-of-range enemies before asking the targeting rule to choose.

```javascript
selectTowerTarget(tower, stats) {
  const candidates = this.enemies.getChildren().filter(enemy =>
    enemy.alive
    && Phaser.Math.Distance.Between(tower.x, tower.y, enemy.x, enemy.y) <= stats.range
  );
  return this.tdRules.pickTarget(candidates, tower.targeting) || null;
}

updateTowers(time) {
  for (const tower of this.towers.getChildren()) {
    const stats = CONFIG.towers[tower.type].levels[tower.level];
    if (time - tower.lastFiredAt < stats.fireIntervalMs) continue;
    const target = this.selectTowerTarget(tower, stats);
    if (!target) continue;

    tower.lastFiredAt = time;
    this.playTowerAttack(tower, target, stats); // asset-backed animation/effect
    this.applyDamage(target, stats.damage);
  }
}

applyDamage(enemy, amount) {
  if (!enemy.alive || amount <= 0) return;
  enemy.hp -= amount;
  if (enemy.hp <= 0) this.killEnemy(enemy);
}

killEnemy(enemy) {
  if (!enemy.alive) return;
  enemy.alive = false;
  const reward = enemy.reward;
  enemy.destroy();
  this.tdState = { ...this.tdState, money: this.tdState.money + reward };
  this.renderTowerDefenceState(this.tdState);
}
```

This is the minimum hitscan attack. If the GDD requires travel time, splash, or status effects, use a bounded projectile/effect pool and route impact back through `applyDamage`; do not create an unbounded object per shot.

## Wave Rewards, Victory + One Scene Update

```javascript
completeWaveIfClear(time) {
  if (this.tdState.status !== 'running') return;
  if (this.spawnQueue.length || this.enemies.getChildren().length) return;

  const wave = this.waveSchedule[this.tdState.waveIndex];
  this.tdState = {
    ...this.tdState,
    money: this.tdState.money + wave.clearReward,
    status: 'ready'
  };
  this.renderTowerDefenceState(this.tdState);

  const isLastWave = this.tdState.waveIndex === this.waveSchedule.length - 1;
  if (isLastWave) this.finishGame(true);
  else if (CONFIG.waves.autoStart) this.startNextWave(time);
}

update(time, delta) {
  if (this.tdState.status === 'complete') return;
  this.updateWave(time);
  this.updateEnemies(delta);
  if (this.tdState.status === 'complete') return;
  this.updateTowers(time);
  this.completeWaveIfClear(time);
}

finishGame(won) {
  if (this.tdState.status === 'complete') return;
  this.tdState = { ...this.tdState, status: 'complete', won };
  this.spawnQueue.length = 0;
  this.disableTowerDefenceInput();
  this.renderTowerDefenceState(this.tdState);
  this.onTowerDefenceComplete({ won, ...this.tdState });
}
```

Manual-wave UI calls `startNextWave(this.time.now)` only while status is `ready`. Wave clear reward is committed once before the status changes; victory occurs only after the last queue and enemy group are empty. Defeat occurs when core lives reach zero.

## Runtime UI

Use manifest/layout assets for the tower roster, selected-tower panel, wave button, and HUD. Render money, lives, wave number, tower names, costs, level stats, and refunds as runtime text with `placeUiText`; bind authored regions with `bindUiSlots`. UI callbacks call `placeTower`, `upgradeTower`, `sellTower`, or `startNextWave` and never mutate `tdState` directly.
