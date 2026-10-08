# Puzzle Implementation Patterns

> **Allowed dependencies:** `sandbox.md`.
>
> Genre: `puzzle`. The dominant loop is receive a discrete action → validate it → transform logical state → resolve until stable → evaluate a goal/failure state. Logic/grid puzzles use `physics: none`; a rigid-body puzzle follows `sandbox.md` for Matter setup but still implements the terminal-state contract below.

> `LEVEL_MODEL=board` consumes the scaffolded board contract, `authored` consumes the Level DSL, and `none` consumes the GDD/`CONFIG` state. Directory existence never selects routing.

## Puzzle Mode Contract

The GDD selects one concrete mode and authors its actions/rules. Do not pretend that one grid operation implements every puzzle:

| Mode family | Valid action | Required transition / settle rule |
|---|---|---|
| Sokoban / movement | step or push | reject blocked pushes; move player/crate atomically; test all goals |
| 2048 / slide-merge | directional slide | compact, merge once per tile, compact, then seeded spawn |
| Match-3 | adjacent swap | reject non-matches; clear, collapse, refill, repeat until stable |
| Tetris-like | move/rotate/drop/tick | validate shape, lock, clear lines, spawn next piece |
| Minesweeper-like | reveal/flag | seeded board, flood empty cells, detect mine or all-safe reveal |

Implement these rules behind `this.puzzleRules.tryAction`, `resolveStep`, and `evaluate`; the Scene loop stays the same.

## Grid Coordinates

Keep values in logical state and visuals in a separate view map. Final tiles/cells come from manifest/layout assets, not hardcoded rectangles.

```javascript
createPuzzleState(board) {
  return {
    board: board.map(row => [...row]),
    phase: 'input',
    moves: 0,
    outcome: null
  };
}

gridToWorld(row, col) {
  const { tileSize, cols, rows } = CONFIG.grid;
  const offsetX = (CONFIG.screen.width - cols * tileSize) / 2;
  const offsetY = (CONFIG.screen.height - rows * tileSize) / 2;
  return {
    x: offsetX + col * tileSize + tileSize / 2,
    y: offsetY + row * tileSize + tileSize / 2
  };
}

isValidCell(row, col) {
  return row >= 0 && row < CONFIG.grid.rows
    && col >= 0 && col < CONFIG.grid.cols;
}
```

## Action → Settle → Goal Loop

`tryAction` is pure: a rejected action returns `{ ok: false, reason }`; an accepted action returns a new state and does not mutate the previous board. `resolveStep` performs one authored cascade/gravity/spawn/clear step.

```javascript
applyPuzzleAction(action) {
  if (this.puzzleState.phase !== 'input') {
    return { ok: false, reason: 'not-accepting-input' };
  }

  const attempt = this.puzzleRules.tryAction(this.puzzleState, action);
  if (!attempt.ok) return attempt;

  let next = {
    ...attempt.state,
    phase: 'settling',
    moves: this.puzzleState.moves + (attempt.moveDelta ?? 1)
  };
  next = this.resolveUntilStable(next);
  const outcome = this.puzzleRules.evaluate(next);
  this.puzzleState = {
    ...next,
    phase: outcome ? 'complete' : 'input',
    outcome: outcome || null
  };

  this.renderPuzzleState(this.puzzleState);
  if (outcome) this.finishPuzzle(outcome);
  return { ok: true, outcome: outcome || null };
}

resolveUntilStable(state) {
  let next = state;
  for (let step = 0; step < CONFIG.puzzle.maxResolveSteps; step += 1) {
    const result = this.puzzleRules.resolveStep(next);
    if (!result.changed) return next;
    next = result.state;
  }
  throw new Error('Puzzle did not settle within the authored guard');
}
```

Chance-based spawns/refills use the authored seeded RNG and store its state; never call `Math.random()`. `maxResolveSteps` is a positive guard authored in `CONFIG`, not a gameplay timer.

## Input + Timed Steps

Register pointer/keyboard handlers once in `create()` and translate them into semantic actions such as `{ type: 'move', direction }`, `{ type: 'swap', from, to }`, or `{ type: 'reveal', row, col }`.

Timed modes dispatch through the same action path:

```javascript
createPuzzleClock() {
  if (!CONFIG.puzzle.tickMs) return;
  this.time.addEvent({
    delay: CONFIG.puzzle.tickMs,
    loop: true,
    callback: () => this.applyPuzzleAction({ type: 'tick' })
  });
}
```

Do not add a second mutation path for timer-driven movement. Pause the clock while `phase !== 'input'` or after completion.

## Terminal State + UI

```javascript
finishPuzzle(outcome) {
  if (this.puzzleFinished) return;
  this.puzzleFinished = true;
  this.disablePuzzleInput();
  this.onPuzzleComplete(outcome); // authored transition / score / retry behavior
}
```

- `evaluate` returns `null`, `'won'`, or an authored failure outcome; the Scene never guesses a goal from visual tiles.
- Reset/undo, hints, score, and move limits are optional authored mechanics and operate on snapshots of logical state.
- Use `placeUiText` for dynamic moves/time/score and `bindUiSlots` for authored board controls; view objects never own puzzle values.
