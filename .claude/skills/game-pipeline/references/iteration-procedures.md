# Iteration Loop — Procedures

## Scope and Authority

This file is the operational appendix to `SKILL.md` §9. It covers the detailed
techniques used inside an iteration; `SKILL.md` remains authoritative for the
state machine, agent routing, counters, final MODE=full confirmation, and ship
conditions.

`SKILL.md` is the workflow SSOT. The live composite membership comes from
`gate-registry.mjs`; query it with
`mcp__gca__gate_check({ gate: "gate_contract" })` and parse the `stdout` JSON.
The table in `.claude/agents/verify-agent.md` under **Composite gates and
evidence map** is an advisory routing index, not workflow authority.

The `iteration_ready` gate's full BLOCKED list is the authoritative issue set.
The critic's `top_issue` controls priority ordering only; it never narrows the
set of blockers that must be fixed.

Each composite decision response also carries a structured `failures[]` — one
record per blocker with its originating `check` plus `axis`/`agent` routing
hints — alongside the human-readable `BLOCKED:` lines in `stderr`. Prefer the
structured list to group blockers by owning `agent` and dispatch fixers in
parallel; the stderr text is the backward-compatible mirror. Fix EVERY blocker
regardless of grouping.

## Interaction Verification — Agent Play (preferred for the interaction dimension)

For the rubric **interaction** dimension, use the play node inside
`run_verify_dag`, not screenshots alone. It plays toward each objective and
reports YES/NO/PARTIAL plus bugs:

```jsonc
mcp__gca__run_verify_dag({
  game_dir: GAME_DIR,
  mode: "iterate",
  url: URL,
  changed: "src/scenes/GameScene.js"
})
```

Read `.playtest/verify/play/agent-play-report.json`: `weighted_accuracy`, per-objective
verdicts, `coverage`, `regressions`, and `bugs`. Treat `bug_suspected` verdicts
and `regressions` as top-priority. Direct `play_game` is reserved for a scoped
diagnostic against an explicitly active recovery epoch.
Numeric play quality is enforced by `ready_to_ship`, not `iteration_ready`.

## Evidence and Regression Tracking

- Compare each fresh `verify-report.json` and `iteration_ready` result with the
  previous round. Any item that flips from pass to blocked is a regression and
  becomes the top-priority fix.
- When an objective or visual check fails, attach the relevant screenshot from
  `.playtest/verify/play/frames/` or the paired paths in
  `.playtest/verify_middle/visual/visual-diff-report.json` to the owning fixer.
  Do not pass only a prose summary.
- Video evidence is valid only from the latest MODE=full release pass. Never use
  a stale recording to diagnose an iterate-round change.

## Level Iteration (cheap path)

When playthrough or rubric flags **pacing, difficulty, spawn density, or boss
timing**, dispatch `level-design-agent MODE=revise` with the complete structured
`ISSUES` set; route a missing runtime prefab implementation to
`implement-agent`. Revise only implicated level files, and do not let the
orchestrator edit either authority directly. Then rerun the no-video
`verify-agent MODE=iterate` pass from `SKILL.md` §9.2.

Do not record after each tweak. Video is absent from iterate rounds and belongs
only to MODE=full release confirmation; rerun full only when a repair makes the
prior release evidence stale.

Spawn ids in JSON (`s1`, `tank_intro`, etc.) are stable handles for surgical JSON Patch tuning later.

## Parallel Fixing

Group ALL blocking issues by their owning agent (SKILL §9.1 maps issue category → agent) and dispatch one fixer per agent type concurrently, each carrying its COMPLETE list:

```
// Failing items from the gate + critic:
// - Interaction: "pause button doesn't work" → owning Scene behavior
// - Visual: "background too flat" → assets/

// Group by owning agent — dispatch in parallel, in a single message:
Agent({ subagent_type: "implement-agent", description: "Fix Scene behavior", prompt: "...pause button..." })
Agent({ subagent_type: "assets-agent", description: "Fix asset-side issues", prompt: "...background asset..." })
```

**Only parallelize if files don't overlap.** After any Implement or Assets fix
that changes integration inputs, join those producers, finalize the manifest
when needed, and run `asset-integration-agent` by itself. It is the project-wide
single writer for final preload, animation, display, and binding wiring and must
never overlap an Implement fixer.

## Video Analysis as Feedback

Applies only to MODE=full release passes—iterate rounds never record, and under
an approved `verify.skip_video` waiver video never runs. When recording runs,
overlay state information on frames for richer feedback:

```javascript
// After recording, extract frames with state overlay
// For each key frame, the analysis sees: the visual + numeric state simultaneously
// This catches issues invisible in either alone:
// - "Score shows 100 but player just died" (state/visual mismatch)
// - "Player appears stuck in corner for 5 seconds" (dead time)
// - "Enemies spawn but never reach player" (broken AI)
```

Recording analysis (`mcp__gca__analyze_video` + the critic) works by:
1. Extract frames at 1fps
2. For each frame, also extract game state at that timestamp
3. Look for state/visual mismatches and gameplay flow issues
4. Report specific timestamps where problems occur

## Completion and Recovery

Use `SKILL.md` §9.4–§9.6 for completion, final confirmation, and cleanup. This
appendix adds two recovery rules:

- If the same issue survives three cycles, change approach or revise the GDD
  rather than repeating the same patch.
- When `pipeline.max_iterations` is exhausted, stop and report the remaining
  gate blockers with the latest critic evidence. The audit writer, state
  transaction, and Stop hook enforce this cap; never silently extend or bypass
  it.

## Code Compression (Refactor Rounds)

When code grows past comfortable complexity (see signs below), fold a refactor into the current fix cycle — `pipeline.max_iterations` is the loop's hard stop cap, NOT a refactor cadence, and refactor rounds count against it like any other cycle:

```
Agent: "Refactor and compress code"
- Read the current codebase
- Identify: accumulated if-else patches, dead code, duplicated logic
- Simplify: extract common patterns, remove dead paths, consolidate
- Constraint: all existing tests must still pass after refactor
- Report: what was simplified, lines before/after
```

**Why:** Without periodic compression, each iteration adds patches that make the codebase harder to maintain. Eventually the fixing sub-agent can't understand the code and makes things worse. Refactoring resets the complexity curve.

Signs code needs compression:
- Same logic duplicated across files
- Deeply nested if-else chains from accumulated bugfixes
- Functions longer than ~80 lines
- Sub-agent reports "confused by code structure" or makes incorrect fixes

## Comprehensive visual loop (global + per-element)

The visual axis (whole-screen + per-element) runs a dedicated fix→re-verify loop
backed by a real image comparison, on a budget separate from
`pipeline.max_iterations`:

- Its budget is `gates.visual.max_rounds`; the general fix-loop budget is
  `pipeline.max_iterations`. Raising one does not affect the other.
- Each round: `mcp__gca__visual_diff({ game_dir: GAME_DIR, url: RUN.url, verify_run_id: RUN.verify_run_id, round: <n> })` pairs each running screen with `design/<screen>.png` and each rendered element with its concept crop, writing the alignments plus `visual-diff-report.json`. Start a new/stale-input loop at 1. The sidecar round controls only the current loop budget; producer-written `visual-diff-history.jsonl` is the counted polish evidence.
- If `aligned:false`, route each visible issue, let the fix land, then re-run with `round = previous sidecar.round + 1`. Runtime roster count/action wiring are proved separately by `gateUiBinding`.
- Stop when `aligned:true` or the sidecar round reaches `gates.visual.max_rounds`. If the pinned Doubao request is unavailable, keep the same round for a later retry, do not append counted history, and do not consume the outer iteration budget or create a blocking known issue. `gateVisualDiff` still blocks missing/stale evidence, verifier errors, and real mismatches.

## Adversarial / Stress Testing

All browser execution goes through `verify-agent` and the `gca` MCP verification
tools. Test sources may use Playwright APIs, but agents never launch Playwright
or Node directly.

- **Quick (every iteration):** rapid inputs, boundary positions, and repeated
  scene transitions.
- **Extended (after interaction or game-loop changes):** continuous multi-wave
  play, repeated actions, idle-then-interact, pause/resume, respawn, and
  menu/shop transitions.
- **Soak (before ship or for intermittent freezes):** run the game loop at
  least three times while sampling FPS, active scene, and object count. Treat a
  sustained object-count rise as a leak, falling FPS as degradation, and
  unchanged progress state as a possible deadlock.

Add generated regression tests under `.playtest/tests/`, execute them through
`mcp__gca__run_tests`, and keep their reports in the canonical `.playtest`
verification tree.

## GDD Revision During Iteration

Revise only when a mechanic is fundamentally flawed, balance is materially wrong,
or the user requests it. Dispatch design-agent as the sole GDD writer, then
dispatch scaffold-agent with `MODE=reproject`, non-empty `CHANGED`, and structured
`ISSUES` before dependent implementation/asset work. Remove only returned
`stale_owned_paths` by consuming Scaffold's returned receipt with
`mcp__gca__delete_scaffold_owned_paths({receipt_id:<stale_receipt_id>})`, then
rerun `scaffold_done`.

Propagation: GDD → projected contracts/config → code/assets → stale tests/evidence
→ fresh verify. Record what changed, why, and what is affected in the GDD
changelog.
