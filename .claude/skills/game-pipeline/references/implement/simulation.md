# Simulation Implementation Patterns

> Genre: `simulation` (build, place, manage, or tune a system whose resources change over fixed ticks). Use `physics: none` unless the GDD explicitly makes movement or rigid-body interaction part of the simulation.

> `LEVEL_MODEL=authored` consumes scenario entities and milestones from the Level DSL. Board/procedural models consume their declared authority; directory existence never selects routing.

The minimum loop is management command → validate the entire transaction → commit atomically → fixed production/consumption tick → evaluate milestones. A timer that only increments money is not a simulation skeleton.

## Serializable State

Keep gameplay values in one serializable model. Sprites, panels, and timers are views/adapters and never own money, inventory, entity level, or completion state.

```javascript
createSimulationState(scenario) {
  return {
    tick: 0,
    status: 'running',
    seed: scenario.seed ?? CONFIG.sim.seed ?? null,
    rngState: scenario.rngState ?? null,
    resources: { ...scenario.startingResources },
    entities: Object.fromEntries(
      scenario.entities.map(entity => [entity.id, { ...entity }])
    ),
    completedMilestones: [],
    commandSequence: 0
  };
}

create() {
  const scenario = this.loadSimulationScenario(); // declared level-model adapter
  this.simState = this.createSimulationState(scenario);
  this.pendingCommands = [];

  this.time.addEvent({
    delay: CONFIG.sim.tickMs,
    loop: true,
    callback: () => this.simTick()
  });

  this.renderSimulationState(this.simState);
}
```

Persist this model when the GDD requires long-running progress. Include an explicit save version and migrate older state before use; never serialize Phaser objects.

## Atomic Management Commands

Placement, upgrade, sale, staffing, and policy changes all use the same command path. The planner validates coordinates/slots, prerequisites, resource costs, and the requested next level before any mutation.

```javascript
queueManagementCommand(command) {
  if (this.simState.status !== 'running') return false;
  this.pendingCommands.push({
    ...command,
    sequence: this.simState.commandSequence + this.pendingCommands.length
  });
  return true;
}

applyManagementCommand(state, command) {
  const plan = this.simRules.planCommand(state, command);
  const sequencedState = {
    ...state,
    commandSequence: Math.max(state.commandSequence, command.sequence + 1)
  };
  if (!plan.ok) {
    return { state: sequencedState, ok: false, reason: plan.reason };
  }

  const next = {
    ...sequencedState,
    resources: this.applyResourceDelta(state.resources, plan.resourceDelta),
    entities: { ...state.entities }
  };

  for (const id of plan.removeEntityIds || []) delete next.entities[id];
  for (const entity of plan.upsertEntities || []) {
    next.entities[entity.id] = { ...entity };
  }
  return { state: next, ok: true };
}

applyResourceDelta(resources, delta) {
  const next = { ...resources };
  for (const [key, amount] of Object.entries(delta || {})) {
    const current = next[key] || 0;
    if (!Number.isFinite(current) || !Number.isFinite(amount)) {
      throw new Error(`Invalid resource delta for ${key}`);
    }
    next[key] = current + amount;
    if (next[key] < 0) throw new Error(`Invalid negative resource: ${key}`);
  }
  return next;
}
```

`simRules.planCommand` is authored for the selected game. A rejected command leaves both resources and entities unchanged, so a failed build/upgrade cannot spend currency or occupy a slot. Sale refunds, unlocks, and capacity changes are also part of the returned plan rather than follow-up side effects.

## Fixed Production / Consumption Tick

Build a deterministic plan in stable priority/id order. Each step consumes its complete input bundle before producing outputs; if any input is unavailable, skip or partially run it only according to the authored rule.

```javascript
settleProduction(state) {
  let resources = { ...state.resources };
  const steps = this.simRules.buildProductionPlan(state)
    .sort((a, b) => a.priority - b.priority || a.id.localeCompare(b.id));

  for (const step of steps) {
    if (!this.simRules.canConsume(resources, step.consume)) continue;
    resources = this.applyResourceDelta(resources, step.resourceDelta);
  }

  return { ...state, resources, tick: state.tick + 1 };
}

simTick() {
  if (this.simState.status !== 'running') return;

  let next = this.simState;
  const commands = this.pendingCommands.splice(0)
    .sort((a, b) => a.sequence - b.sequence);
  for (const command of commands) {
    next = this.applyManagementCommand(next, command).state;
  }

  next = this.settleProduction(next);
  next = this.simRules.applyAuthoredTickEvent(next); // seeded/no-op when disabled
  const result = this.simRules.evaluateMilestones(next);
  this.simState = {
    ...next,
    completedMilestones: result.completedMilestones,
    status: result.outcome ? 'complete' : 'running'
  };

  this.renderSimulationState(this.simState);
  if (result.outcome) this.finishSimulation(result.outcome);
}
```

Do not call an unconditional random-event helper. Chance is an explicit GDD axis; enabled events use seeded RNG stored in the serializable state so the same state and commands reproduce the same tick.

## Placement + Panel Input

- A spatial simulation translates a clicked grid cell/build slot into `{ type: 'place', entityType, location }`.
- A panel-managed simulation translates buttons into commands such as `{ type: 'upgrade', entityId }`, `{ type: 'sell', entityId }`, or `{ type: 'set-policy', key, value }`.
- Both paths call `queueManagementCommand`; UI callbacks never subtract resources or modify entity levels directly.
- Pause/speed controls change the tick scheduler, not production values. Define whether queued commands can be submitted while paused.

Use manifest/layout assets for panels and build slots, `placeUiText` for live resources/tick/status, and `bindUiSlots` for controls. Refresh views from `simState` after each command batch/tick rather than storing duplicate UI state.

## Milestones + Terminal State

```javascript
finishSimulation(outcome) {
  if (this.simFinished) return;
  this.simFinished = true;
  this.pendingCommands.length = 0;
  this.disableSimulationInput();
  this.onSimulationComplete(outcome); // authored results/retry/next scenario
}
```

Milestones may unlock entities without ending the scenario; terminal outcomes may be budget, population, delivery, survival-time, or failure targets. `evaluateMilestones` owns those rules—never infer completion from a HUD string or a missing sprite.
