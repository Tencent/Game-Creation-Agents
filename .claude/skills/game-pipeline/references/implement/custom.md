# Custom Implementation Patterns

> **Allowed dependencies:** `platform.md`, `character-controller.md`,
> `bullet_hell.md`, `roguelike.md`, `sandbox.md`, `puzzle.md`, `card.md`,
> `idle.md`.
>
> Genre: `custom` — the **engineering fallback**, chosen by Startup routing metadata ONLY when none of the 10 genres fit. There is no fixed gameplay skeleton, but routing is already fixed: Implement receives immutable `INTERACTION_PROFILE`, `QUALITY_PROFILE`, and `LEVEL_MODEL` values. The GDD supplies the custom mechanics and must agree with those dispatched facts.

> **If `LEVEL_MODEL=authored`**, drive setup from the Level DSL. `custom` does
> not imply authored levels; directory existence never selects routing.

## Validate the Dispatched Engineering Profile

`custom` has no genre default. Implement-agent **is dispatched** the required
`INTERACTION_PROFILE`; consume it directly and never infer a replacement from
the genre, directory contents, or implementation preference. Before writing,
require exact agreement between:

- dispatched `INTERACTION_PROFILE`;
- GDD §0.2 Physics, Interaction & Collision Model; and
- `.playtest/inputs/interaction-contract.json` `interaction_profile`.

If they disagree, return a structured contract issue to Design/Scaffold rather
than silently choosing one. The GDD and scaffolded contract are consistency
checks and implementation detail authorities—not sources for re-routing.

| Dispatched `INTERACTION_PROFILE` | Physics setup | Borrow patterns from |
|---|---|---|
| `arcade-gravity` | Arcade physics, gravity from `CONFIG.physics.gravity`; import `CharacterController` from `src/utils/character-controller.js` when the player jumps | `platform.md` |
| `arcade-no-gravity` | Arcade physics, `gravity.y: 0`; free top-down / twin-stick movement | `bullet_hell.md` / `roguelike.md` |
| `matter` | Matter physics; rigid bodies, constraints, materials | `sandbox.md` |
| `none` | No physics engine; state machine + tick/UI-driven logic | `puzzle.md` (grid) / `card.md` / `idle.md` (panels) |

Read camera routing as two independent GDD fields:

- `camera.view`: `side-on`, `top-down`, or `screen-space` selects the spatial/view skeleton;
- `camera.behavior`: `fixed`, `follow`, `auto-scroll`, or `pan` selects how that view moves.

Match `camera.view`, `camera.behavior`, and the `control` axis to the closest
borrowed skeleton, then diverge only where the authored mechanics require it.
Do not collapse view and behavior into one enum or use either to overwrite
`INTERACTION_PROFILE`.

## Build strictly from the GDD contract

Because there is no genre template to lean on, the GDD owns custom gameplay
intent and tuning; immutable dispatch fields and scaffolded machine contracts
retain their own authority. Do not invent mechanics:

- **§0.2 Mechanics Contract** — implement every MUST row; never add a mechanic the GDD does not state, and never implement a MUST_NOT.
- **§0.2 interaction model** — wire the `interaction-contract.json` layers/matrix exactly (scaffold derived it from the GDD). For Arcade profiles only, use `registerInteractions(this, ...)` from `src/display-utils.js` and `tagEntityInteractionLayer` for entities spawned after registration. For `matter`, implement the same authored layer/matrix semantics through Matter APIs; do not call the Arcade registrar. For `none`, do not create a physics system or registrar.
- **§3 CONFIG** — all tuning numbers come from `CONFIG`, never hardcoded.
- **§8 roadmap** — follow the scene/build order.

## Combat / FX / UI

- Melee or attack collisions → import the canonical `hitboxOverlapsHurtbox` helper from `src/display-utils.js`; its CONFIG-backed pushbox/hurtbox/hitbox contract owns the rectangle math.
- VFX follows `QUALITY_PROFILE` + GDD (particle recipe, mask/shader, flipbook, or none).
- Value-bearing / multi-slot UI widgets → bind via `placeUiText` (anchors) + `bindUiSlots` (slots) per the §2E.1 spec, same as every genre.

## Common mistakes

- **Waiting for a genre skeleton** — there isn't one; after routing validation, compose the implementation plan from the authored GDD mechanics.
- **Re-deriving routing** — `INTERACTION_PROFILE`, `QUALITY_PROFILE`, and `LEVEL_MODEL` are immutable dispatch inputs; validate them instead of replacing them.
- **Flattening camera routing** — keep `camera.view` and `camera.behavior` separate so view and motion variants compose correctly.
- **Adding genre-default extras** — a `custom` game only has the mechanics the GDD lists; borrowed skeletons supply structure, not extra features.
