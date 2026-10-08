# Mechanics Contract Guide — Authoring GDD §0.2

§0.2 is the **"plays-like" contract** — the mechanics truth, equal in standing to the visual truth. It must be concrete enough that:

- **scaffold-agent** turns its Controls + Physics/Interaction/Collision model into `.playtest/inputs/interaction-contract.json`, and its Objectives into `.playtest/inputs/objectives.json`,
- **implement-agent** turns its behaviors/physics/rules into runnable code (colliders wired from the contract matrix),
- **verify-agent** checks the running game against it (gameplay alignment + the `interaction_contract` gate bundle).

Author it in Phase 3 from the Prompt, concept-derived elaboration, and tagged
genre defaults. A concept may create coherent gameplay where the Prompt is
silent; nothing may contradict explicit requirements or exclusions.

## Sourcing & precedence

Every row carries a **Source** and a **MUST / MUST_NOT / SHOULD** tag. Resolve conflicts with:

```
Prompt [MUST/MUST_NOT] > other explicit Prompt prose > concept-derived addition
> tagged genre-standard default
```

- Mirror each `PROMPT.md` `[MUST]` into a §0.2 row tagged MUST.
- Use a genre default only to close an actual gap, label its source, and record
  tempting-but-wrong defaults in §0.2 MUST_NOT.

## The sub-sections (fill every one)

1. **Physics, Interaction & Collision Model** — the structured contract scaffold turns into `interaction-contract.json`. Fill:
   - `Physics` = `arcade-gravity | arcade-no-gravity | matter | none` (must match §1 **and** Startup routing metadata's `interaction_profile`) + the movement model string.
   - **Layers** — name every kind of thing that interacts (`player, enemy, terrain, pickup, …`; a pointer game may be just `star, pointer`). A layer is "a class of thing that collides", not a single entity.
   - **Matrix** — for each interacting layer pair pick exactly one verb: `collide` (rigid, can stand/block), `collide_oneway` (jump-through platform), `overlap` (detect only — pickups, hitbox↔hurtbox, hazards), `pick` (pointer hit — select/click/drag), `ignore` (explicit no-interaction, e.g. own bullets vs. own ship). This is the collision topology that implement used to redraw from scratch every game — author it once here.
   - **Per-entity hit shape** — `{shape: rect|circle, body: dynamic|static|kinematic|sensor|none, fit: bbox}` + optional `bullet`/`min_hit_px`. **Two PHYSICS number classes**: shapes are MEASURED (`fit: bbox` → the `display-params` body at integrate, never a literal size here); layers/matrix/character feel are AUTHORED topology/tuning.
   - **Character feel** (platform, incl. auto-scroll variant, and side beat_em_up only) — `coyote_ms ~100, jump_buffer_ms ~120, variable_jump, max_slope_deg, one_way_drop`. These are authored numbers; the shared `CharacterController` reads them.
   - **World** — `bounds` (which edges collide) + `gravity` (matches §1).

2. **Controls & Objectives** — the two tables scaffold consumes:
   - **Controls**: one row per input the player can issue — `Action | Input (key/mouse/touch) | Effect | MUST/MUST_NOT | Source`. Be literal about input (if the Prompt calls for mouse path-drawing, say so — do not write arrow keys).
   - **Objectives**: one row per thing the player must be able to achieve — `Objective | Reachable by | Success signal | Source`. These become play-agent objectives; each must be reachable and have an observable success signal.

3. **Core Rules** — the gameplay rules (`bonded atoms move together`, `waves of N`, …), each tagged.

4. **Pacing / Difficulty Curve** — one-line summary (full curve in §6).

5. **Win / Lose Conditions** — exact end states + visible signals (drives §6 + verify win checks).

6. **Entity Behaviors** — one row per entity: the behavior (movement + primary action), tagged + sourced. Full per-entity detail goes in §4; this is the contract §4 must honor.

7. **Game Feel / Juice intent** — Prompt-required, concept-derived, or tagged
   genre-standard feedback (full table in §5).

8. **MUST_NOT** — forbidden wrong guesses, each with the reasoning that rules it out.

## Quality bar (what makes §0.2 actionable)

- **Controls are unambiguous** — an implementer can bind every input without guessing. Inputs use concrete Phaser-style names where possible (`ArrowLeft`, `Space`, `pointerdown`).
- **Objectives are testable** — each has a concrete success signal (scene transition, score threshold, target cleared) that play-agent can assert, not a vibe.
- **Every row traces to a source** — Prompt, concept, or tagged genre default.
  Untraceable rows are fabrication; drop them.
- **Behaviors match §4** — the entity behaviors here and the per-entity specs in §4 say the same thing.
- **No unexplained additions** — a dash absent from Prompt and concept exists
  only when intentionally chosen and tagged as a genre default; otherwise put
  the tempting wrong guess in MUST_NOT.

## How it flows downstream (interface)

| §0.2 content | Consumer | Becomes |
|---|---|---|
| Controls table + Physics/Interaction/Collision model | scaffold-agent | `.playtest/inputs/interaction-contract.json` (`actions` + `layers/matrix/world/character/entities`) |
| Objectives table | scaffold-agent | `.playtest/inputs/objectives.json` (play-agent objectives) |
| Behaviors + rules | implement-agent | scene code, AI; colliders wired from `interaction-contract.json` matrix |
| Win/lose + objectives | verify-agent | gameplay-alignment checks |
| Layers/matrix + hit shapes + character feel | verify-agent | `interaction_contract` gate bundle (topology/wiring/world-bounds/tunnel/prefab + fit) |

(The exact JSON shapes live under `references/scaffold/schemas/`; §0.2 supplies
the content and scaffold-agent projects it.)
