# Rubric Guide — Quality Evaluation

## What is a Rubric

A rubric is a scaffold-projected checklist of game-specific statements derived
from the GDD. It helps authors and reviewers connect intent to canonical
evidence; it is not an evaluator output or a release score. Composite membership
is machine-owned and queryable through `gate_check({ gate: "gate_contract" })`;
the table in `verify-agent.md` is only an advisory gate-to-evidence routing
index.

## Three Dimensions and Evidence Owners

| Dimension | What it measures | Canonical evidence owner |
|-----------|------------------|--------------------------|
| **Intention** | Does the game do what the GDD says it should? | Formal `run_tests`, `play_game`, and gameplay alignment |
| **Visual** | Does it look polished and match the art direction? | `visual_diff` visual/element outputs; recording and critic may interpret them |
| **Interaction** | Do inputs produce correct responses? Is gameplay functional? | Formal tests, `play_game`, and applicable interaction/UI/physics gates |

Layout coordinates are deterministic facts owned by `capture_layout` →
`materialize_layout_alignment` → `gateLayoutAlignment`, not visual rubric
scores. Human reviewers and the critic interpret producer evidence; they do not
replace its owner or make the composite decision.

## Generating a Rubric from GDD

Read the GDD and extract concrete, checkable items:

**From GDD Section 1 (Technical Architecture):**
- All scenes in the scene flow are reachable
- Scene transitions work bidirectionally where specified

**From GDD Section 2 (Visual Style & Art Production Plan):**
- Art style matches description (e.g., "pixel art" → no smooth gradients)
- Each asset in the asset table is visually present in-game
- Background has the described depth/layers
- UI elements listed in GDD are present and readable
- Every §2.0.2 Scene Layout Design element renders at its measured position (the
  *lays-out-like* axis is deterministically enforced by
  `gateLayoutAlignment` / Axis 5; do not duplicate it as a visual score)

**From GDD Section 4 (Entity & Scene Architecture):**
- Each entity behavior described in GDD is functional
- Each interaction (collision, overlap, trigger) produces the stated effect
- Each win/lose condition works

**From GDD Section 6 (Level & Content Design):**
- All levels/waves/content are playable
- Difficulty progression matches description

## Rubric Format

```jsonc
{
  "version": 1,
  "generated_from": "GAME_DESIGN.md v1",
  "intention": [
    "Game boots and reaches title screen without errors",
    "Player can start a game from the title screen",
    "[Mechanic X] works as described: [specific behavior]",
    "Level can be completed (win condition reachable)",
    "Game over condition triggers correctly"
  ],
  "visual": [
    "Art style matches GDD: [specific style description]",
    "All [N] asset types from GDD are visible (not placeholders)",
    "Background has [described layers/depth]",
    "UI shows [listed HUD elements]",
    "Animations exist for [key actions from GDD]",
    "Visual hierarchy keeps gameplay, HUD, and calls to action readable",
    "No visual artifacts, clipping, or misalignment"
  ],
  "interaction": [
    "[Input A] produces [state change B] (verify via state-extractor)",
    "[Entity collision] triggers [effect] (verify via state-extractor)",
    "Scene transition [X→Y] works (verify active scene after action)",
    "No stuck states reachable via normal play (random input for 30s, game still responsive)",
    "[Resource/score] updates correctly on [action] (verify numeric change)"
  ]
}
```

## Verifying Interaction Items with State Extractor

The key insight: **don't just look at screenshots to judge interaction — extract state before and after an action.**

```javascript
// Pattern: verify action → state change
const before = await page.evaluate(EXTRACTOR);
// ... perform action (keyboard, mouse, wait) ...
const after = await page.evaluate(EXTRACTOR);

// Compare specific values
const scoreBefore = before.scenes.GameScene.custom.score;
const scoreAfter = after.scenes.GameScene.custom.score;
// Assert: score changed as expected
```

This catches bugs that are invisible in screenshots:
- Button click that doesn't actually change state
- Collision that renders visually but doesn't modify health
- Scene transition that appears to work but resets state incorrectly

## Ownership and Changes

`.playtest/inputs/rubric.json` is a scaffold-owned build input. It is frozen
throughout an active verify epoch; a verifier or reviewer must not edit it to
match the current implementation or preserve old evidence.

- A bug fix keeps the current rubric and adds or updates a regression test.
- A genuine intent/scope change starts in the GDD, then runs scaffold
  `MODE=reproject` so projected inputs are regenerated.
- Any projected-input change refreshes the build digest and requires a fresh
  verify epoch with newly generated evidence.

Do not add rubric items opportunistically during verification. If a discovered
issue changes product intent rather than implementation correctness, route it
through the GDD → scaffold reproject workflow first.

## Evidence and Release Authority

`rubric.json` is a scaffold-owned checklist input, not a score report. The
current pipeline has no canonical rubric-score producer, result schema, or gate.
Although `gates.rubric.*` keys exist in `gca.config.yaml`, no release gate
consumes them. Do not present per-dimension scores or rubric thresholds as ship
authority.

Use rubric statements to author and interpret the existing evidence:

- intention: formal tests, `play_game`, and gameplay alignment;
- visual: `visual_diff` visual/element artifacts, with critic and recording as
  supporting interpretation;
- interaction: formal tests, `play_game`, and applicable interaction/UI/physics
  gates;
- layout position: Axis 5 layout evidence only, not a second visual judgment.

Only the current composite gate result—`iteration_ready` or
`ready_to_ship`—is authoritative. Numeric interaction quality is enforced
by `gatePlayQuality` inside `ready_to_ship` (or an explicit
`gate_check({ gate: "play_quality", verify_run_id: VERIFY_RUN_ID })` diagnosis),
using `gates.play_quality_min_accuracy`; it is not an `iteration_ready` member.

## Using Rubric in Pipeline

An optional human review may summarize identity-bound evidence against the
rubric. Any PASS/FAIL annotations are review notes only: they do not replace
producer artifacts, create a release score, or decide ship status.

**Optional review prompt:**
```
You are reviewing playground/<game-slug> against its rubric.
Rubric: [paste rubric.json]

For each item:
1. Locate the current producer-owned artifact with matching run identity.
2. Interpret that artifact; do not create replacement evidence.
3. Record PASS or FAIL as a review note with the artifact path and reason.

List failing items and their evidence. Do not issue a release score.
```

For visual items, `mcp__gca__visual_diff` is the only producer of
`alignment/visual/alignment.json`, `alignment/element/alignment.json`, and
`verify_middle/visual/visual-diff-report.json`. A reviewer or critic must never
hand-author or overwrite those files. Missing sidecars or invalid
`source`/run-identity provenance remain blocking evidence errors.

The reviewer identifies issues only. Fixes run through the normal iteration
workflow and must regenerate the required evidence before gate evaluation.
