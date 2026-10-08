# Agent Play — Intelligent Closed-Loop Verification

> The play-agent drives the game in a perceive → decide → act → observe loop and
> verifies objectives, instead of running a blind pre-written interaction script.
> It is the "smart" counterpart to `test-runner.mjs --solo --script interact.mjs`.

## Why

Scripted `interact.mjs` fires a fixed input sequence that is blind to live state;
random stress input finds crashes but not logic bugs; pinned-Doubao video
analysis judges *offline* and never touches the controls. The play-agent closes
the loop: it looks at the screen + engine state every step, decides the next
action, actuates it through a per-game control-map, runs bug oracles, and judges
each objective YES / NO / PARTIAL with evidence.

Design influences: VideoGameBench (ReAct + pause-while-thinking + stuck detection),
TITAN (bug oracles + diagnostics), WebGen-Bench ({operation, expected_result}
test cases judged YES/NO/PARTIAL with weighted accuracy), a build-health
gate, GameSense (optional hierarchical control).

## Files

| File | Role |
|------|------|
| `mcp__gca__play_game` | MCP tool that runs the play-agent (the normal entry point) |
| `gca-mcp/.../runners/verify/play-agent.mjs` | The closed-loop harness (Playwright + LLM + oracles + report) |
| `gca-mcp/.../runners/verify/oracles.mjs` | Deterministic bug oracles (crash, stuck, invariants, timing) |
| `gca-mcp/.../runners/verify/llm-client.mjs` | Shared pinned-Doubao ARK client |
| `gca-mcp/.../runners/verify/state-extractor.mjs` | Gray-box observation (reused) |

> The scripts live in `gca-mcp/src/gca_mcp/runners/verify/` and are invoked
> through `mcp__gca__play_game`. The bundled runner is an internal implementation
> path; agents do not invoke it directly.

Composite membership is machine-owned and queryable through
`gate_check({ gate: "gate_contract" })`; the table in `verify-agent.md` is only
an advisory gate-to-evidence routing index.

## Inputs — per game, under `<GAME_DIR>/.playtest/inputs/`

### `interaction-contract.json` — how to interact

Maps semantic actions the agent can choose to concrete input primitives (the
`actions` block; the play-agent also consumes `canvas`, `gameContext`, and
optional `player`). The canonical shape is
`../scaffold/schemas/interaction-contract.schema.json`; complete examples live
under `../scaffold/examples/contracts/`. Older games named this
`control-map.json`; that legacy path still resolves.

```json
{
  "schema_version": 1,
  "genre": "puzzle",
  "interaction_profile": "none",
  "scheme": "pointer",
  "canvas": { "designWidth": 800, "designHeight": 600 },
  "gameContext": "Select Start to enter the puzzle board.",
  "actions": {
    "start": {
      "primitive": "pointer_click",
      "target": "start-button",
      "x": 400,
      "y": 480
    }
  },
  "world": {},
  "layers": ["ui"],
  "entities": {
    "start-button": {
      "layer": "ui", "body": "none", "shape": "rect", "fit": "bbox",
      "min_hit_px": 40
    }
  },
  "matrix": [["ui", "ui", "pick"]]
}
```

Primitives currently handled by `actuate()`: `pointer_move`, `pointer_click`,
`tap`, `pointer_down`, `pointer_up`, `key_press` (optional `hold_ms`),
`key_down`, `key_up`, and `wait` (`ms`). `tap` and `pointer_click` perform a
full click; a bare `pointer_down` currently also performs a full click for
compatibility, while `pointer_up` releases only.

The interaction-contract schema does not enumerate primitive values, and the
current pointer-topology gate does not classify `tap`. This list records runner
behavior; it does not expand the schema/gate contract or promise drag/hold
semantics.

Coordinate values accept: a number (always game-space, converted via the canvas
box), `"NN%"` of canvas, or `"center"`. List `argsFromLLM` for params the model supplies.
`target` is descriptive metadata only; it does not locate or aim at an entity.
Pointer actions must provide `x`/`y`, request them through `argsFromLLM`, or
intentionally use the canvas-center fallback.

Optional top-level `"player"` (string or array of texture/name substrings, e.g.
`"player"` or `["paddle","ball"]`) helps spatial-region coverage identify the
controllable object. It is purely a coverage hint — omit it and coverage still
works for everything else.

Author the control-map at scaffold time from GDD §0.2 Mechanics Contract
(Controls table). Keep the action vocabulary small and semantic (what a player
does), not raw keys.

### `objectives.json` — what to verify (WebGen-Bench style)

Atomic `{operation, expected_result}` cases. Optional `success` predicate gives a
deterministic ground-truth signal checked before another model call; a satisfied
predicate also bypasses the final LLM judge. `max_steps` caps each objective and
`verify.play_max_wall_seconds` (240 by default) caps the whole producer. The canonical shape is
`../scaffold/schemas/objectives.schema.json`.

`verify.play_game_timeout_seconds` is the outer process timeout and must remain
at least 15 seconds above `play_max_wall_seconds`; this lets the Node watchdog
seal runtime identity plus completed/current objective findings before Python
can terminate the process.

```json
{
  "schema_version": 1,
  "objectives": [
    { "id": "start_game", "operation": "Start the game from the title.",
      "expected_result": "GameScene active with player + HUD.",
      "max_steps": 4, "success": { "activeScene": "GameScene" } },
    { "id": "score", "operation": "Eat smaller fish to score.",
      "expected_result": "score rises above 0.",
      "max_steps": 30, "success": { "scene": "GameScene", "custom": "score", "op": ">", "value": 0 } }
  ]
}
```

`success` predicate forms:
- `{ "activeScene": "GameScene" }`
- `{ "scene": "GameScene", "custom": "score", "op": ">", "value": 0 }` (dotted paths ok)
- `{ "scene": "GameScene", "group": "enemyFish", "field": "active", "op": "<", "value": 5 }`

These forms are exclusive and closed. The runner validates the complete
objectives document before launching Chromium or calling a model; an unknown
field exits immediately with code 2. Migrate legacy prose-shaped entries:

- `setup.seedBoard` → constructive `setup.state`/`setup.eval`, or a snapshot;
- `success.state` → one supported `custom` predicate per focused objective;
- `success.read` / free-form `success.predicate` → a supported comparator when
  possible, otherwise a deterministic Playwright test.

Do not keep an unsupported key as documentation inside the JSON contract: it
would otherwise consume the full play budget without ever becoming an
executable predicate.

Derive objectives from the GDD §0 core loop + §0.2 Objectives table + the
rubric's `interaction` items (scaffold-agent writes the first cut at scaffold time).

#### Canonical contract and fail-fast boundary

Scaffold must project a valid `objectives.json` containing a real
`start_game` objective and the core GDD acceptance objectives.
`gateProjectedContracts` enforces that input before canonical verification.

If the file is missing, unreadable, empty, lacks `start_game`, or violates the
closed DSL, the runner exits with code 2 before browser startup or provider
selection. Do not replace it with synthetic free play: repair/migrate the
scaffold contract, then start a fresh verify epoch so the corrected file is
covered by the build digest.

### `setup` — warp straight to the state under test (efficiency)

Each objective may declare an optional `setup` so the agent **jumps directly to
the target state instead of replaying from boot** (same principle as the test
runner: state injection, never replay from boot). This skips menu/level grind and
lets you verify deep states (e.g. a boss fight, a late level) in a few steps.

```json
{ "id": "boss_fight",
  "operation": "Defeat the level-3 boss.",
  "expected_result": "boss.hp reaches 0",
  "setup": {
    "scene": "GameScene",            // scene.start(scene, data)
    "stop": ["TitleScene"],          // scenes to stop first
    "data": { "level": 3 },          // passed to scene.start
    "state": { "GameScene": { "playerHp": 100 } },  // shallow-assign scene props after start
    "eval": "const s=window.__phaserGame.scene.getScene('GameScene'); s.spawnBoss();" // advanced
  },
  "success": { "scene": "GameScene", "custom": "boss.hp", "op": "<=", "value": 0 } }
```

All `setup` fields are optional: `scene`, `stop`, `data`, `state`, `eval`,
`js` (path to a module exporting `default async (page)`), and `snapshot`
(see below). `stop` and `data` require `scene`; without it the runner has no
`scene.start` operation to consume them and contract validation fails.
**Rule:** `setup` establishes the *precondition* only — never
inject the success state itself, or the objective passes trivially. State
injection also lets you verify a mechanic in isolation even when the natural
path to it is broken/slow (e.g. test "eating works" by warping to a larger
size, separate from a starting-size balance bug).

### Save-state snapshots — `setup.snapshot` (opt-in, full-state restore)

`scene`/`state`/`eval` build a precondition *constructively*. When you instead
want to restore a **complete, real gameplay state** (e.g. a state captured mid-
playthrough, or a reproduced bug state), use `setup.snapshot` — a path to a
snapshot JSON file (or an inline object). The seed handler calls the game's
`window.__phaserGame.__restore(snapshot)` hook:

```json
{ "id": "boss_phase2_dps",
  "operation": "Keep damaging the boss in phase 2.",
  "expected_result": "boss.hp keeps dropping",
  "setup": { "snapshot": ".playtest/inputs/scenarios/boss-phase2.json" },
  "success": { "scene": "GameScene", "custom": "boss.hp", "op": "<", "value": 500 } }
```

This is **opt-in**: it only works if the game exposes the save-state hooks. Phaser
has no generic serialize/deserialize (live object graph + closures + physics/tween
internals are not restorable), so the game restores **gameplay-semantic plain
data** and lets `create()` rebuild the live objects. Convention:

```js
// main.js — alongside `window.__phaserGame = game`:
game.__snapshot = () => {
  const out = { version: 1, active: [], scenes: {} };
  for (const s of game.scene.getScenes(true)) {
    out.active.push(s.scene.key);
    if (typeof s.getSaveState === 'function') out.scenes[s.scene.key] = s.getSaveState();
  }
  return out;                       // MUST be JSON-serializable plain data
};
game.__restore = (snap) => {
  if (!snap) return;
  game.scene.getScenes(true).forEach((s) => game.scene.stop(s.scene.key));
  for (const key of snap.active || Object.keys(snap.scenes || {})) {
    game.scene.start(key);
    const s = game.scene.getScene(key);
    if (s && typeof s.loadSaveState === 'function') s.loadSaveState(snap.scenes[key]);
  }
};

// each gameplay scene — store/rebuild only semantic data (no live objects):
getSaveState() {
  return { score: this.score, lives: this.lives, level: this.level,
           player: { x: this.player.x, y: this.player.y, hp: this.player.hp },
           enemies: this.enemies.getChildren().map((e) => ({ type: e.enemyType, x: e.x, y: e.y, hp: e.hp })) };
}
loadSaveState(s) {
  this.score = s.score; this.lives = s.lives; this.level = s.level;
  this.player.setPosition(s.player.x, s.player.y); this.player.hp = s.player.hp;
  s.enemies.forEach((e) => this.spawnEnemy(e.type, e.x, e.y, e.hp)); // reuse the factory
}
```

**Capturing a snapshot**: call `window.__phaserGame.__snapshot()` (e.g. inside a
test/interact script via `page.evaluate`) and write the result to
`.playtest/inputs/scenarios/<name>.json`; or hand-author the JSON to match
`loadSaveState`'s shape. If the game has no `__restore`, the `snapshot` seed is
ignored with a warning — fall back to constructive `scene`/`state`.

## Running

Canonical use is the play node owned by `run_verify_dag`:

```jsonc
mcp__gca__run_verify_dag({
  game_dir: GAME_DIR,
  mode: "iterate",
  url: URL,
  changed: "src/scenes/GameScene.js"
})
```

Direct `play_game` is a scoped diagnostic/recovery primitive and must reuse an
explicitly active epoch's exact URL and run ID. Raw runner invocation is omitted:
an incomplete identity fails closed and cannot produce canonical evidence.

Provider selection (in `llm-client.mjs`) routes the pinned Doubao 2.0 model
through ARK. Alternate models/providers are rejected. With no usable transport it
falls back to the heuristic policy so the harness always produces a report.

## The loop (per step)

1. Pause the Phaser loop (`game.loop.sleep()`), unless `--realtime` — so slow LLM
   latency can't invalidate the action (VideoGameBench-Lite).
2. Observe: screenshot + `extractStateCompact` (gray-box). `--vision-only` drops
   the state text.
3. Oracles: crash / stuck / invariant (NaN, entity leak, fps floor) on full state.
   A frozen-state finding requires both compact state and screenshot pixels to
   remain unchanged, then ends that objective before another model request.
4. If the success predicate is already true, finish deterministically.
   Otherwise decide: model returns `{ thought, action, args, memory, status }`
   (strict JSON).
5. Resume, actuate via control-map, let the game run `--action-delay` ms.
6. Log to `trajectory.jsonl`; stop on `status:done`, success predicate, or step cap.
7. Reuse a satisfied deterministic predicate; otherwise judge the objective
   YES/NO/PARTIAL from initial/final state + final screenshot, including blocked
   runs so actuation failures can still surface as suspected bugs.

## Outputs — `<GAME_DIR>/.playtest/verify/play/`

- `trajectory.jsonl` — per-run, per-step record (thought, action, oracle hits).
  It is cleared at each `play_game` start and is not cross-run history.
- `agent-play-report.json` — `build_health`, `weighted_accuracy`,
  `objective_scores`, `objectives_detail`, `bugs` (oracle + judge), coverage,
  frame paths, and runtime identity.
- `agent-play-history.jsonl` — append-only cross-run summaries containing
  scores, weighted accuracy, build health, coverage, timestamps, and runtime
  identity. MCP stamps the latest producer row with the current
  pipeline/build/run identity. Only valid producer rows count toward
  `gatePolishRounds`; never hand-write or copy history to manufacture a round.
- `frames/` — per-step screenshots.

`weighted_accuracy = (Σ yes·1 + partial·0.5) / verified_objectives`.
Exit code is non-zero if the Build-Health gate fails (booted, no fatal errors,
≥1 non-empty screenshot) — so gate-check / CI can react.

## Integration points

- Iterate rounds: run `play_game` each cycle for interaction evidence,
  regressions, and fixer feedback. `iteration_ready` includes
  `gameplay_alignment`, but not the numeric `play_quality` check.
- Critic: reads `agent-play-report.json` as evidence.
- Release: `ready_to_ship` adds `gatePlayQuality`, which enforces
  `build_health.pass`, `weighted_accuracy >=
  gates.play_quality_min_accuracy`, regressions, blocking play bugs, and
  freshness. Diagnose it explicitly with
  `mcp__gca__gate_check({ gate: "play_quality", verify_run_id:
  RUN.verify_run_id })`.
