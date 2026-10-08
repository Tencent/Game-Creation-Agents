# Roguelike Implementation Patterns

> **Allowed dependencies:** `puzzle.md`.
>
> Genre: `roguelike` (run-based explore/fight/build, run reset plus optional meta-progression). Real-time top-down uses `arcade-no-gravity`; a `control: discrete-step` game uses the grid model from `puzzle.md` instead. Run rules, procedural constraints, drops, build slots, reset conditions, and meta rewards come from the GDD and `CONFIG`.

> **If `LEVEL_MODEL=authored`**, consume rooms/encounters from the Level DSL. `procedural` uses the seeded rules below; `board` consumes its declared board authority. Directory existence never selects routing.

## Movement + Aim Helpers

Input booleans come from the scaffolded interaction contract. These helpers do not own a lifecycle method; compose them in the single real-time `update(time, delta)` at the end of this file.

```javascript
applyTopDownMovement(actor, input, speed, out) {
  out.set(
    (input.right ? 1 : 0) - (input.left ? 1 : 0),
    (input.down ? 1 : 0) - (input.up ? 1 : 0)
  );
  if (out.lengthSq() > 1) out.normalize();
  actor.body.setVelocity(out.x * speed, out.y * speed);
  return out;
}

aimAtPointer(actor, pointer, out) {
  this.cameras.main.getWorldPoint(pointer.x, pointer.y, out);
  return Phaser.Math.Angle.Between(actor.x, actor.y, out.x, out.y);
}
```

Reuse one `Phaser.Math.Vector2` for movement and one for pointer world coordinates. Never aim with raw screen-space pointer coordinates.

## Seeded Run State

Use a run-local `RandomDataGenerator`, never `Math.random()`. Store its serializable state after every random decision so save/load resumes the same sequence.

```javascript
createRunState(seed) {
  this.runRng = new Phaser.Math.RandomDataGenerator([String(seed)]);
  return {
    version: CONFIG.run.version,
    seed,
    floor: CONFIG.run.startFloor,
    hp: CONFIG.player.maxHealth,
    inventory: [],
    relics: [],
    eventSeq: 0,
    events: [],
    rngState: this.runRng.state(),
    status: 'active'
  };
}

restoreRunState(saved) {
  this.assertValidRunState(saved);
  this.runRng = new Phaser.Math.RandomDataGenerator([String(saved.seed)]);
  this.runRng.state(saved.rngState);
  this.runState = saved;
}
```

Persist the run separately from permanent meta state. Derived combat stats are recalculated from base stats + inventory/relics after load; do not repeatedly bake multipliers into saved base values.

## Deterministic Rooms

Derive a stable sub-seed for each procedural floor/room so topology does not change when unrelated drop/event calls are added.

```javascript
generateFloor(floorIndex) {
  const floorRng = new Phaser.Math.RandomDataGenerator([
    `${this.runState.seed}:floor:${floorIndex}`
  ]);
  return this.buildFloorFromAuthoredRules(
    CONFIG.run.generation, floorIndex, floorRng
  );
}
```

Generation validates required exits, reachable objectives, spawn clearance, and authored encounter constraints before committing the floor.

## Drop + Event Stream

All chance uses `runRng`. Record an ordered event with a sequence number and the post-decision RNG state; gameplay systems consume events instead of re-rolling the same outcome.

```javascript
rollDrop(tableId, sourceId) {
  const table = CONFIG.drops[tableId];
  if (!table) throw new Error(`Unknown drop table: ${tableId}`);
  const itemId = this.pickWeightedDrop(table, this.runRng.frac());
  const event = {
    seq: this.runState.eventSeq + 1,
    type: 'drop',
    sourceId,
    itemId,
    rngState: this.runRng.state()
  };
  this.runState = {
    ...this.runState,
    eventSeq: event.seq,
    events: [...this.runState.events, event],
    rngState: this.runRng.state()
  };
  return event;
}
```

Bound or checkpoint the event history using the authored persistence policy; never let a long run grow an unbounded save payload.

## Run Build

Items/relics enter the build through validated state transitions. A rejected pickup/equip changes neither inventory nor derived stats.

```javascript
addRelic(relicId) {
  const definition = CONFIG.relics[relicId];
  if (!definition) return { ok: false, reason: 'unknown-relic' };
  if (!this.canAddRelic(this.runState, definition)) {
    return { ok: false, reason: 'build-restriction' };
  }
  this.runState = {
    ...this.runState,
    relics: [...this.runState.relics, relicId]
  };
  this.rebuildDerivedStats();
  return { ok: true };
}
```

## Run Reset + Meta Progression

Calculate an authored result before mutation. Reset run-only state; persist only declared permanent rewards/unlocks. A game without meta-progression still resets the run but omits `metaState` updates.

```javascript
finishRun(outcome) {
  if (this.runState.status !== 'active') return false;
  const result = this.calculateAuthoredRunResult(this.runState, outcome);
  this.runState = { ...this.runState, status: outcome };

  if (CONFIG.meta?.enabled) {
    this.metaState = this.applyMetaReward(this.metaState, result.metaReward);
    this.saveMetaState();
  }
  this.clearRunSave();
  this.scene.start(result.nextScene, { outcome, summary: result.summary });
  return true;
}
```

`saveMetaState`, run saves, and migrations are versioned. Never carry run HP, temporary items, room events, or the current RNG stream into a fresh run unless the GDD explicitly says so.

## One Real-Time Scene Update

The real-time variant composes movement, optional pointer aim, AI, and terminal checks once. The discrete-step variant follows `puzzle.md` and must not also run this continuous movement path.

```javascript
update(time, delta) {
  if (this.runState.status !== 'active') return;
  const input = this.readMovementActions();
  this.applyTopDownMovement(
    this.player, input, CONFIG.player.speed, this.moveVector
  );
  if (CONFIG.run.aimMode === 'pointer') {
    const angle = this.aimAtPointer(
      this.player, this.input.activePointer, this.aimPoint
    );
    this.player.setRotation(angle);
  }

  this.updateEnemies(time, delta);
  this.consumeRunEvents();
  if (this.runState.hp <= 0) this.finishRun('defeat');
}
```

Initialize reusable vectors and all input handlers once in `create()`. Do not add another `update()` for aim or attacks.
