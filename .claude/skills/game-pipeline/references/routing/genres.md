# Startup Genre Routing

This file selects one implementation-reference/runtime-loop family plus the
design axes needed downstream. It does not choose level dispatch, tuning values,
canvas dimensions, asset map modes, controllers, or implementation techniques.

## 1. Required outputs

Startup produces:

- `GENRE`: one slug from §3;
- `DESIGN_AXES`: the six fields in §2;
- `INTERACTION_PROFILE`: copied from `DESIGN_AXES.physics`;
- `QUALITY_PROFILE` and `LEVEL_MODEL`: selected through
  `../quality-profiles.md`;
- `LEVEL_DESIGN_REQUIRED`: exactly `LEVEL_MODEL == "authored"`.

All state values use lowercase slugs. Never output placeholders such as
`explicit`, `varies`, or `per GDD`.

## 2. Six design axes

| Axis | Value |
|---|---|
| `control` | Concrete input model: keyboard, pointer, drag, tap, discrete-step, etc. |
| `camera` | `{ view: side-on \| top-down \| screen-space, behavior: fixed \| follow \| auto-scroll \| pan }` |
| `physics` | `arcade-gravity \| arcade-no-gravity \| matter \| none` |
| `chance` | Random/seeded generation, events, draw/shuffle, or `none` |
| `resource` | Relevant runtime resources, or `none` |
| `progression` | Stages, waves, run reset/meta, prestige, continuous, or `none` |

`camera.view` and `camera.behavior` are independent. For example, a platformer
may be `{view:"side-on", behavior:"follow"}` or
`{view:"side-on", behavior:"auto-scroll"}`.

## 3. Routing families

Choose the family whose runtime loop and implementation skeleton dominate. These
are routing families, not exhaustive marketing genres.

| Genre | Slug | Dominant runtime loop | Valid interaction candidates | Common level models (non-exhaustive) |
|---|---|---|---|---|
| Platform | `platform` | Traverse terrain by running/jumping | `arcade-gravity` | `authored`, `procedural` |
| Beat 'Em Up | `beat_em_up` | Close-range combat against rooms/waves/crowds | side-on: `arcade-gravity`; top-down: `arcade-no-gravity` | `authored`, `procedural` |
| Bullet Hell | `bullet_hell` | Dodge/read dense projectile patterns | `arcade-no-gravity` | `authored`, `procedural` |
| Roguelike | `roguelike` | Run-based exploration/build with reset or meta progression | real-time: `arcade-no-gravity`; grid: `none` | `procedural`, `board`, `authored` |
| Puzzle | `puzzle` | Reach a logic/spatial goal state | logic: `none`; rigid-body puzzle: `matter` | `board`, `authored`, `none` |
| Tower Defence | `tower_defence` | Place/upgrade units against advancing paths | `arcade-no-gravity` | `authored`, `procedural`, `board` |
| Physics Sandbox | `sandbox` | Build/manipulate objects under rigid-body physics | `matter` | `none`, `procedural`, `authored` |
| Simulation | `simulation` | Manage and grow an evolving system | choose one legal §2 physics value from actual mechanics | `board`, `procedural`, `authored`, `none` |
| Card | `card` | Draw/play/manage cards, hand, deck, or board | `none` | `board`, `authored`, `none` |
| Idle | `idle` | Accumulate resources, buy upgrades, prestige | `none` | `none`, `board`, `authored` |
| Custom fallback | `custom` | No family above supplies the required runtime skeleton | choose one legal §2 physics value | choose one legal level model |

Genre selects only the implementation reference and dominant runtime loop. The
interaction and level columns are examples, never applicability or dispatch
allowlists. `LEVEL_MODEL` is the sole level-applicability/dispatch authority:
`authored` is legal for every genre and produces `levels/stage_*.json`; `board`
defines a board/deck/grid runtime contract and does not create `levels/*.json`
or dispatch level design.
`quality_profile` is independent of genre.

## 4. Ambiguity rules

- Prefer the specialized runtime skeleton: card play → `card`; path defence →
  `tower_defence`; dense projectile reading → `bullet_hell`.
- Use `roguelike` only when run generation/build/reset materially defines the
  architecture; a card-led run remains `card`.
- Side-on and top-down beat-'em-up variants differ through `camera` and
  `physics`, not through a new genre.
- Endless runners remain `platform` with `camera.behavior:"auto-scroll"`.
- Use `sandbox` for rigid-body construction/manipulation, not every game that
  happens to use physics.
- If no implementation family fits without distortion, use `custom` instead of
  forcing a misleading genre.

Startup stops after routing. Gameplay numbers, controller selection, map
representation, combat geometry, resolution, and other craft decisions belong to
design/scaffold/implement authorities.
