# Idle Implementation Patterns

> Genre: `idle` (Idle Games in GameMaker, also called incremental or clicker games; no physics, fixed camera). The core loop is accumulate resources → buy production/upgrades → unlock long-run growth. IGDB has no separate Idle genre, so the pipeline keeps the explicit `idle` route. Clicking, offline progress, automation, and prestige are optional and must be authored by the GDD.

> The GDD and `CONFIG` own resource names, generator definitions, cost curves, tick size, offline cap, save key/version, upgrades, and reset rules. This reference supplies no balance defaults.

## Economy State

Keep one serializable model. Run values reset during an authored prestige; permanent values survive. Never store Phaser objects, timers, or functions in the save payload.

```javascript
createIdleState(rules, nowMs) {
  const state = {
    version: rules.saveVersion,
    run: {
      currency: rules.startingCurrency,
      lifetimeCurrency: 0,
      generators: Object.fromEntries(
        Object.keys(rules.generators).map(id => [id, 0])
      )
    },
    lastSavedAtMs: nowMs
  };
  if (rules.upgrades) state.run.upgrades = [];
  if (rules.prestige) {
    state.permanent = { prestigeCurrency: 0, prestigeCount: 0 };
  }
  return state;
}
```

## Fixed-Step Production

Use the same production function for online ticks and offline progress. `authoredProductionMultiplier` composes only upgrades/prestige declared by the GDD and otherwise returns the multiplicative identity `1`.

```javascript
productionPerSecond(state = this.idleState) {
  let rate = 0;
  for (const [id, owned] of Object.entries(state.run.generators)) {
    const definition = this.idleRules.generators[id];
    if (!definition) throw new Error(`Unknown generator: ${id}`);
    rate += owned * definition.productionPerSecond;
  }
  return rate * this.authoredProductionMultiplier(state);
}

advanceEconomy(state, elapsedMs) {
  if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return state;
  const earned = this.productionPerSecond(state) * (elapsedMs / 1000);
  return {
    ...state,
    run: {
      ...state.run,
      currency: state.run.currency + earned,
      lifetimeCurrency: state.run.lifetimeCurrency + earned
    }
  };
}

update(time, delta) {
  const nowMs = Date.now();
  const elapsedMs = Math.max(0, nowMs - this.lastWallClockMs);
  this.lastWallClockMs = nowMs;
  this.economyAccumulatorMs += elapsedMs;

  const stepMs = this.idleRules.tickMs;
  const steps = Math.floor(this.economyAccumulatorMs / stepMs);
  if (steps === 0) return;
  this.economyAccumulatorMs -= steps * stepMs;
  this.idleState = this.advanceEconomy(this.idleState, steps * stepMs);
  this.refreshIdleUi();
}
```

Initialize `lastWallClockMs` and `economyAccumulatorMs` in `create()`. Validate `tickMs > 0` and `maxOfflineMs >= 0`; never use `setInterval` or make production frame-dependent.

## Offline Progress + Persistence

Use an authored, game-specific save key. Parse, migrate one version at a time, and validate before applying capped offline progress. Keep malformed/future saves intact instead of silently overwriting them.

```javascript
loadIdleState(nowMs, storage = globalThis.localStorage) {
  try {
    const raw = storage.getItem(this.idleRules.saveKey);
    if (raw === null) return this.createIdleState(this.idleRules, nowMs);
    const state = this.migrateIdleSave(JSON.parse(raw));
    this.assertValidIdleSave(state);
    const offlineMs = Math.min(
      Math.max(0, nowMs - state.lastSavedAtMs),
      this.idleRules.maxOfflineMs
    );
    return { ...this.advanceEconomy(state, offlineMs), lastSavedAtMs: nowMs };
  } catch (error) {
    this.persistenceWriteBlocked = true;
    this.reportPersistenceWarning(error);
    return this.createIdleState(this.idleRules, nowMs);
  }
}

saveIdleState(nowMs, storage = globalThis.localStorage) {
  if (this.persistenceWriteBlocked) return false;
  const snapshot = { ...this.idleState, lastSavedAtMs: nowMs };
  try {
    storage.setItem(this.idleRules.saveKey, JSON.stringify(snapshot));
    this.idleState = snapshot;
    return true;
  } catch (error) {
    this.reportPersistenceWarning(error);
    return false;
  }
}
```

`assertValidIdleSave` rejects future versions, invalid timestamps, unknown generator IDs, duplicate upgrades, and non-finite/negative resources. Only an explicit reset/recovery action clears `persistenceWriteBlocked`.

Register autosave once with `this.time.addEvent` and save again on Scene shutdown.

## Atomic Purchases

Validate ID, prerequisites, cap, and full cost before committing. A failed purchase changes nothing.

```javascript
buyGenerator(id) {
  const definition = this.idleRules.generators[id];
  if (!definition) return { ok: false, reason: 'unknown-generator' };
  const owned = this.idleState.run.generators[id];
  if (definition.maxOwned !== undefined && owned >= definition.maxOwned) {
    return { ok: false, reason: 'cap-reached' };
  }
  if (!this.meetsGeneratorPrerequisites(this.idleState, definition)) {
    return { ok: false, reason: 'prerequisite-missing' };
  }
  const cost = definition.baseCost * (definition.costGrowth ** owned);
  if (!Number.isFinite(cost) || cost < 0) {
    throw new Error(`Invalid authored cost: ${id}`);
  }
  if (this.idleState.run.currency < cost) {
    return { ok: false, reason: 'insufficient-currency' };
  }

  this.idleState = {
    ...this.idleState,
    run: {
      ...this.idleState.run,
      currency: this.idleState.run.currency - cost,
      generators: { ...this.idleState.run.generators, [id]: owned + 1 }
    }
  };
  this.refreshIdleUi();
  return { ok: true, cost };
}
```

Use the same transaction shape for upgrades.

## Prestige Reset

Only include prestige when authored. Calculate eligibility/reward before mutation, create a fresh run, and carry permanent values forward.

```javascript
prestige() {
  const reward = this.calculateAuthoredPrestigeReward(this.idleState);
  if (reward < this.idleRules.prestige.minimumReward) return false;
  const permanent = {
    ...this.idleState.permanent,
    prestigeCurrency: this.idleState.permanent.prestigeCurrency + reward,
    prestigeCount: this.idleState.permanent.prestigeCount + 1
  };
  this.idleState = {
    ...this.createIdleState(this.idleRules, Date.now()), permanent
  };
  this.saveIdleState(Date.now());
  this.refreshIdleUi();
  return true;
}
```

## Economy UI

- Render panels, generator rows, upgrades, and prestige controls from the declared source strategies in the manifest and layout contract.
- Use `placeUiText` for dynamic currency/rate/price/count values and `bindUiSlots` for authored purchase rows.
- Refresh bound text after a tick or transaction; do not rebuild the dashboard every frame.
- Formatting is display-only; never feed rounded strings back into economy state.
