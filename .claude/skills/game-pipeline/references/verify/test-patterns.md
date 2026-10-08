# Test Patterns Reference

Templates and examples for generating game verification tests. Each test is an ES module that the test-runner executes independently in a fresh browser page.

**All examples below use generic placeholder names.** When generating real tests, replace scene names, property names, and values with those from the actual game's GDD and code.

Composite membership is machine-owned and queryable through
`gate_check({ gate: "gate_contract" })`; the table in `verify-agent.md` is only
an advisory gate-to-evidence routing index.

## Execution Contract

Formal tests run only after integration. The canonical suite is a checkpointed
node inside the verify DAG:

```text
PASS = mcp__gca__run_verify_dag({
  game_dir: GAME_DIR,
  mode: "iterate",
  url: URL
})
```

For a scoped diagnostic only, call `run_tests` against the exact active epoch
URL and `verify_run_id`; it writes non-canonical scoped output. The MCP wrapper
owns browser startup, navigation, runtime identity, and result paths.
Test modules receive its fresh `page`; they do not launch a browser or navigate.
The raw runner is an internal maintainer path and is not release evidence.

### Scoped diagnostics versus canonical evidence

Calls that use `filter`, `id`, `changed`, or `solo`, plus any formal call whose
`tests` path is not the exact canonical `.playtest/tests/` directory, are
targeted diagnostics. They set `scope.full_suite: false` and write scoped/solo
result artifacts; those artifacts cannot satisfy `tests_passing` or a composite
gate. Never copy, rename, or edit them into canonical `test-results.json`.

After targeted diagnosis and any fix, run a canonical DAG pass again:

```text
mcp__gca__run_verify_dag({
  game_dir: GAME_DIR,
  mode: "iterate",
  url: URL
})
```

Before a composite gate, the current result must report formal mode,
`scope.full_suite: true`, empty selectors, and `summary.skip === 0`.

## Test Module Format

Every test file exports a default object:

```javascript
export default {
  // Required
  name: 'Short descriptive name',

  // Optional metadata
  id: '003',                              // Unique ID (usually from filename)
  tags: ['unit', 'input'],                // For filtering: smoke | unit | regression | invariant | scenario
  covers: ['src/scenes/GameScene.js'],    // Best-effort scoped diagnostic hints

  // Each phase is optional, but at least one phase must exist
  setup: async (page) => { /* prepare game state */ },
  act:   async (page) => { /* perform actions */ },
  assert: async (page) => { /* check results */ },
};
```

### Phase contracts

| Phase | Purpose | Rules |
|-------|---------|-------|
| `setup` | Get game into the right state | Use `page.evaluate()` to inject state. **Never** play through the entire game to reach a state — skip directly. |
| `act` | Perform the action being tested | Keyboard, mouse, or state mutation. Keep it minimal — one logical action per test. |
| `assert` | Verify the result | Read state via `page.evaluate()` and throw on mismatch. Include expected vs actual in error message. |

The runner executes whichever phases are present and rejects a module only when
all three are absent. A setup-only or act-only module can therefore report PASS
without proving behavior. Formal behavioral tests should include a meaningful
`assert`; this is an authoring requirement, not a separate phase gate.

### Assertion pattern

Always throw `Error` with a message that shows **expected vs actual**:

```javascript
assert: async (page) => {
  const state = await page.evaluate(() => {
    const scene = window.__phaserGame.scene.getScene('GameScene');
    return { score: scene.score, lives: scene.lives };
  });
  if (state.score !== 10) throw new Error(`score: expected 10, got ${state.score}`);
  if (state.lives !== 2)  throw new Error(`lives: expected 2, got ${state.lives}`);
},
```

### State injection and scene isolation

Use `page.evaluate()` to jump directly to a precondition. Stop every active
scene except explicitly allowed overlays, start the target, then wait for an
observable active-scene condition. Do not rely on a fixed sleep to establish
scene identity.

The runner does not inject the helper below. Every generated test module that
calls `startSceneIsolated` must copy this complete definition into that same
file before its default export. Do not place a standalone `.js`/`.mjs` helper
under `.playtest/tests/`: the runner discovers every such file as a test module.
Later examples omit the repeated definition only for readability.

```javascript
async function startSceneIsolated(page, target, data = {}, keepActive = []) {
  await page.evaluate(({ target, data, keepActive }) => {
    const game = window.__phaserGame;
    const keep = new Set(keepActive);
    for (const scene of game.scene.getScenes(true)) {
      if (!keep.has(scene.scene.key)) game.scene.stop(scene.scene.key);
    }
    game.scene.start(target, data);
  }, { target, data, keepActive });

  await page.waitForFunction(({ target, keepActive }) => {
    const game = window.__phaserGame;
    const allowed = new Set([target, ...keepActive]);
    const active = game.scene.getScenes(true).map((scene) => scene.scene.key);
    return active.includes(target) && active.every((key) => allowed.has(key));
  }, { target, keepActive });
}

setup: async (page) => {
  await startSceneIsolated(page, 'GameScene', { level: 1 });
},
```

For scenes that receive data from previous scenes, inject the data directly.
List an overlay in `keepActive` only when parallel activity is part of the
contract:

```javascript
setup: async (page) => {
  await startSceneIsolated(page, 'ResultScene', {
    score: 500,
    won: true,
    timeElapsed: 42,
  });
},
```

---

## Test Types

### 1. Smoke Tests

Verify the game boots and scenes exist. Scaffold generates these modules; they
first execute after integration, as part of the canonical unfiltered verify
suite—not as a separate post-scaffold browser step.

```javascript
// smoke/001_boots_without_error.js
export default {
  name: 'Game boots without JS errors',
  tags: ['smoke'],
  covers: ['index.html', 'src/main.js'],
  setup: async (page) => {
    await page.waitForTimeout(2000);
  },
  act: async () => {},
  assert: async (page) => {
    const errors = await page.evaluate(() => window.__gameVerifyErrors || []);
    if (errors.length > 0) throw new Error(`Console errors: ${errors.join('; ')}`);

    const hasGame = await page.evaluate(() => !!window.__phaserGame);
    if (!hasGame) throw new Error('window.__phaserGame not found');
  },
};
```

```javascript
// smoke/002_scenes_registered.js
export default {
  name: 'All expected scenes are registered',
  tags: ['smoke'],
  covers: ['src/main.js'],
  setup: async () => {},
  act: async () => {},
  assert: async (page) => {
    const keys = await page.evaluate(() => {
      const game = window.__phaserGame;
      return game.scene.keys ? Object.keys(game.scene.keys) : [];
    });
    // Fill in expected scenes from the GDD scene registry
    const expected = ['BootScene', 'TitleScene', 'GameScene', 'GameOverScene'];
    for (const k of expected) {
      if (!keys.includes(k)) throw new Error(`Scene '${k}' not registered. Found: ${keys.join(', ')}`);
    }
  },
};
```

### 2. Unit Tests — Input

One test per input binding. Verify input → state change causality.

```javascript
// unit/input/003_key_moves_player.js
// Include the complete startSceneIsolated definition above in this module.
export default {
  name: 'Input key moves player in expected direction',
  tags: ['unit', 'input'],
  covers: ['src/scenes/GameScene.js'],
  setup: async (page) => {
    await startSceneIsolated(page, 'GameScene', { level: 1 });
  },
  act: async (page) => {
    const before = await page.evaluate(() => {
      const s = window.__phaserGame.scene.getScene('GameScene');
      return { x: s.player.x, y: s.player.y };
    });
    page.__testBefore = before;

    // Press a movement key — adapt to the game's actual bindings
    await page.keyboard.down('ArrowRight');
    await page.waitForTimeout(300);
    await page.keyboard.up('ArrowRight');
    await page.waitForTimeout(100);
  },
  assert: async (page) => {
    const after = await page.evaluate(() => {
      const s = window.__phaserGame.scene.getScene('GameScene');
      return { x: s.player.x, y: s.player.y };
    });
    if (after.x <= page.__testBefore.x) {
      throw new Error(`Player did not move right: before.x=${page.__testBefore.x}, after.x=${after.x}`);
    }
  },
};
```

### 3. Unit Tests — Mechanics

Verify a specific game mechanic in isolation.

```javascript
// unit/mechanics/006_action_changes_state.js
// Include the complete startSceneIsolated definition above in this module.
export default {
  name: 'Game action produces expected state change',
  tags: ['unit', 'mechanics'],
  covers: ['src/scenes/GameScene.js'],
  setup: async (page) => {
    await startSceneIsolated(page, 'GameScene', { level: 1 });
  },
  act: async (page) => {
    // Perform the action being tested — adapt to the game
    // Example: click at a position, press a key, wait for a timer, etc.
    await page.mouse.click(200, 300);
    await page.waitForTimeout(1000);
  },
  assert: async (page) => {
    const result = await page.evaluate(() => {
      const s = window.__phaserGame.scene.getScene('GameScene');
      // Read whatever state the action should have changed
      return { stateValue: s.someProperty };
    });
    if (result.stateValue !== expectedValue) {
      throw new Error(`Expected ${expectedValue}, got ${result.stateValue}`);
    }
  },
};
```

### 4. Unit Tests — Scene Transitions

```javascript
// unit/flow/010_title_to_gameplay.js
// Include the complete startSceneIsolated definition above in this module.
export default {
  name: 'Title screen transitions to gameplay on start',
  tags: ['unit', 'flow'],
  covers: ['src/scenes/TitleScene.js'],
  setup: async (page) => {
    await startSceneIsolated(page, 'TitleScene');
  },
  act: async (page) => {
    // Trigger start — adapt to the game's actual start mechanism
    await page.keyboard.press('Space');
    await page.waitForTimeout(1500);
  },
  assert: async (page) => {
    const activeScenes = await page.evaluate(() =>
      window.__phaserGame.scene.getScenes(true).map(s => s.scene.key)
    );
    if (activeScenes.includes('TitleScene') && activeScenes.length === 1) {
      throw new Error(`Still on TitleScene after start action. Active: ${activeScenes.join(', ')}`);
    }
  },
};
```

### 5. Regression Tests

Created after a bugfix. Name references the bug.

```javascript
// regression/015_fix_boundary_stuck.js
// Include the complete startSceneIsolated definition above in this module.
export default {
  name: 'Entity does not get stuck on boundary',
  tags: ['regression'],
  covers: ['src/scenes/GameScene.js'],
  bug: 'Entity gets permanently stuck when moving into world boundary',
  setup: async (page) => {
    await startSceneIsolated(page, 'GameScene', { level: 1 });
    // Position entity near the boundary
    await page.evaluate(() => {
      const s = window.__phaserGame.scene.getScene('GameScene');
      s.player.x = 5; // Near left edge
    });
  },
  act: async (page) => {
    // Push into boundary then try to move away
    await page.keyboard.down('ArrowLeft');
    await page.waitForTimeout(500);
    await page.keyboard.up('ArrowLeft');
    await page.keyboard.down('ArrowRight');
    await page.waitForTimeout(500);
    await page.keyboard.up('ArrowRight');
    await page.waitForTimeout(100);
  },
  assert: async (page) => {
    const pos = await page.evaluate(() => {
      const s = window.__phaserGame.scene.getScene('GameScene');
      return { x: s.player.x };
    });
    if (pos.x < 50) {
      throw new Error(`Entity stuck near boundary: x=${pos.x} (should have moved away)`);
    }
  },
};
```

### 6. Invariant Tests

Check properties that must always hold during gameplay. Acceptance tests use a
fixed action sequence. Optional fuzz diagnostics must use an explicit seed and
include both the seed and replayable action list in any failure; never rerun
random input until it happens to pass.

```javascript
// invariant/020_state_bounded.js
// Include the complete startSceneIsolated definition above in this module.
const ACTIONS = [
  'ArrowUp', 'ArrowRight', 'Space', 'ArrowDown', 'ArrowLeft',
  'ArrowRight', 'Space', 'ArrowUp', 'ArrowLeft', 'ArrowDown',
];

export default {
  name: 'Game state values stay within valid bounds',
  tags: ['invariant'],
  covers: ['src/scenes/GameScene.js'],
  setup: async (page) => {
    await startSceneIsolated(page, 'GameScene', { level: 1 });
  },
  act: async (page) => {
    const violations = [];
    for (let i = 0; i < ACTIONS.length; i++) {
      const key = ACTIONS[i];
      await page.keyboard.press(key);
      await page.waitForTimeout(150);

      const state = await page.evaluate(() => {
        const s = window.__phaserGame.scene.getScene('GameScene');
        if (!s) return null;
        return { score: s.score };
      });
      if (state && state.score < 0) violations.push({ tick: i, key, state });
    }
    page.__invariantRun = { actions: ACTIONS, violations };
  },
  assert: async (page) => {
    const run = page.__invariantRun;
    if (!run || run.violations.length > 0) {
      throw new Error(
        `Invariant violation: expected non-negative score; ` +
        `actions=${JSON.stringify(run?.actions ?? ACTIONS)}; ` +
        `violations=${JSON.stringify(run?.violations ?? 'missing samples')}`
      );
    }
  },
};
```

### 7. Invariant — Performance

Generic test modules may validate that telemetry is present and finite, but they
must not invent release FPS thresholds. Release authority is
`record_with_state` → `performance-summary.json` →
`gate_check({ gate: "performance", verify_run_id: VERIFY_RUN_ID })`, using
`gates.performance` from `gca.config.yaml`. Add a numeric assertion here only
for an explicit, game-specific SLA.

```javascript
// invariant/021_fps_telemetry_valid.js
// Include the complete startSceneIsolated definition above in this module.
export default {
  name: 'FPS telemetry remains valid during representative gameplay',
  tags: ['invariant', 'perf'],
  covers: [],
  setup: async (page) => {
    const gameplay = await page.evaluate(() => {
      const scenes = window.__phaserGame.scene.getScenes(false).map(s => s.scene.key);
      return scenes.find(k => !['BootScene', 'TitleScene'].includes(k)) || scenes[0];
    });
    await startSceneIsolated(page, gameplay);
  },
  act: async (page) => {
    const samples = [];
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(500);
      const fps = await page.evaluate(() => Math.round(window.__phaserGame.loop.actualFps));
      samples.push(fps);
    }
    page.__fpsSamples = samples;
  },
  assert: async (page) => {
    const samples = page.__fpsSamples;
    if (!Array.isArray(samples) || samples.length === 0 ||
        samples.some((value) => !Number.isFinite(value) || value <= 0)) {
      throw new Error(`Expected finite positive FPS samples, got ${JSON.stringify(samples)}`);
    }
  },
};
```

### 7.1 Invariant — Determinism (same input → same end state)

For games whose `INTERACTION_PROFILE` uses fixed-step Arcade or Matter, replaying
the same input from the same state must end identically. Skip when
`INTERACTION_PROFILE=none`; genre does not decide applicability.

```javascript
// invariant/022_deterministic_replay.js
// Include the complete startSceneIsolated definition above in this module.

// Replay a fixed script from a fixed start and snapshot the end state.
const SCRIPT = ['ArrowRight','ArrowRight','Space','ArrowRight','Space','ArrowLeft'];
async function playOnce(page) {
  await startSceneIsolated(page, 'GameScene', { level: 1, seed: 1 });
  for (const key of SCRIPT) { await page.keyboard.press(key); await page.waitForTimeout(120); }
  await page.waitForTimeout(400);
  return page.evaluate(() => {
    const s = window.__phaserGame.scene.getScene('GameScene');
    const p = s.player?.body;
    // Round to absorb sub-pixel float noise; compare the gameplay-relevant state.
    return JSON.stringify({
      x: p ? Math.round(p.x) : null, y: p ? Math.round(p.y) : null,
      score: s.score ?? null,
    });
  });
}

export default {
  name: 'Same input sequence yields same end state (fixed timestep)',
  tags: ['invariant', 'determinism'],
  covers: ['src/scenes/GameScene.js'],
  // First run establishes the baseline end state.
  setup: async (page) => {
    page.__run1 = await playOnce(page);
  },
  // Second run replays the identical script from the identical start.
  act: async (page) => {
    page.__run2 = await playOnce(page);
  },
  // The two end-state snapshots must match exactly.
  assert: async (page) => {
    if (page.__run1 !== page.__run2) {
      throw new Error(`Non-deterministic replay:\n  run1=${page.__run1}\n  run2=${page.__run2}\n  → check physics.arcade.fixedStep/fps, dt-scaled movement, and seed any Math.random in gameplay.`);
    }
  },
};
```

> Requires a deterministic start: the scene should accept a `seed` and route gameplay RNG through it (or avoid `Math.random()` in the simulation). If the game can't be seeded, note it as a limitation rather than asserting a hard pass — but flag unseeded RNG in physics as an issue, since it defeats reproducible verification.

### 8. Scenario Tests

Longer tests that verify a complete gameplay flow.

```javascript
// scenario/040_complete_level.js
// Include the complete startSceneIsolated definition above in this module.
export default {
  name: 'First level is completable',
  tags: ['scenario'],
  covers: ['src/scenes/GameScene.js'],
  setup: async (page) => {
    await startSceneIsolated(page, 'GameScene', { level: 1 });
  },
  act: async (page) => {
    // Replace with the deterministic GDD-defined completion path and controls.
    for (let i = 0; i < 30; i++) {
      const keys = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];
      await page.keyboard.press(keys[i % keys.length]);
      await page.waitForTimeout(300);
    }
    // Wait for any win/lose timers
    await page.waitForTimeout(5000);
  },
  assert: async (page) => {
    const scenes = await page.evaluate(() =>
      window.__phaserGame.scene.getScenes(true).map(s => s.scene.key)
    );
    const state = await page.evaluate(() => {
      const s = window.__phaserGame.scene.getScene('GameScene');
      // Replace levelComplete with the real GDD/code completion signal.
      return s ? { complete: s.levelComplete, failed: s.levelFailed } : null;
    });
    if (state?.complete !== true) {
      throw new Error(
        `Level completion: expected complete=true, got ${JSON.stringify(state)}; ` +
        `activeScenes=${scenes.join(',')}`
      );
    }
  },
};
```

If a natural completion path cannot be scripted reliably with fixed input, keep
the formal test focused on a controlled win-condition transition and move
end-to-end reachability to a `play_game` objective with a deterministic
`success` predicate. Never substitute “did not crash” for completion or inject
the success state itself.

---

## Manifest Format

The manifest tracks test metadata for coverage and best-effort scoped
diagnostics. Each `file` is relative to `.playtest/tests/`:

```jsonc
// .playtest/tests/manifest.json
{
  "version": 1,
  "generated": "2025-05-17T12:00:00Z",
  "tests": [
    {
      "id": "001",
      "file": "smoke/001_boots_without_error.js",
      "name": "Game boots without JS errors",
      "created_at": "phase:scaffold",
      "tags": ["smoke"],
      "covers": ["index.html", "src/main.js"]
    },
    {
      "id": "015",
      "file": "regression/015_fix_boundary_stuck.js",
      "name": "Entity does not get stuck on boundary",
      "created_at": "bugfix:boundary-stuck",
      "tags": ["regression"],
      "covers": ["src/scenes/GameScene.js"],
      "bug_description": "Entity gets stuck when moving into world boundary"
    }
  ]
}
```

When generating new tests, always update manifest.json with the new entry.

`covers` and the `changed` selector are optimization hints, not an isolation
contract. The current lookup can fail to match a manifest entry and safely run
additional tests or the full set; it does not promise a minimal selection.
Use that scoped result to diagnose the change, then run the canonical,
unfiltered full suite before any composite gate. Improving lookup precision is
a non-blocking runner optimization, not a reason to promote scoped artifacts.

---

## State Reading Cheatsheet

Common state reads for assertions (all via `page.evaluate`):

```javascript
// Active scene keys
window.__phaserGame.scene.getScenes(true).map(s => s.scene.key)

// Get a specific scene
window.__phaserGame.scene.getScene('GameScene')

// Player/entity position
const s = window.__phaserGame.scene.getScene('GameScene');
({ x: s.player.x, y: s.player.y })

// Group counts (if game uses groups)
({ total: s.enemies.getLength(), active: s.enemies.countActive(true) })

// Custom scene state (game-specific properties)
// Read whatever the game stores on the scene instance
({ score: s.score, level: s.currentLevel })

// FPS
Math.round(window.__phaserGame.loop.actualFps)

// Console errors (requires error capture installed by runner)
window.__gameVerifyErrors

// All registered scene keys
Object.keys(window.__phaserGame.scene.keys)
```

---

## LLM Generation Guidelines

When generating tests from GDD + code:

1. **One test = one behavior.** Don't test multiple mechanics in the same test.
2. **Skip, don't play.** Use `page.evaluate()` to jump to any state. Never replay the entire game to reach a test condition.
3. **Expected vs actual.** Every assertion error message must show both values.
4. **Tag appropriately.** `smoke` for boot/structure, `unit` for single mechanics, `regression` for bugfixes, `invariant` for always-true properties (including `determinism` for fixed-timestep physics games — see §7.1), `scenario` for multi-step flows.
5. **List covers.** Name every source file the test depends on. This supports
   best-effort scoped diagnosis; it does not replace the final full suite.
6. **IDs are sequential.** Use 3-digit zero-padded IDs matching filename prefix.
7. **Read the GDD and code.** Don't assume scene names, property names, or game mechanics. Every test must be based on the actual game's structure.
