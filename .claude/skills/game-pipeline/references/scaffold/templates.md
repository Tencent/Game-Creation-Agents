# Scaffold Templates

This file contains only project skeletons and projection rules. Machine-readable
contracts live in `schemas/`; worked examples live in `examples/`.

## Project skeleton

```text
<GAME_DIR>/
├── index.html
├── package.json
├── src/
│   ├── main.js
│   ├── config.js
│   ├── display-utils.js             # canonical shared display/geometry helper
│   ├── utils/
│   │   ├── particle-recipes.js      # only when the GDD declares particle VFX
│   │   └── character-controller.js  # only for side-view platform controllers
│   └── scenes/
│       └── <SceneKey>.js
├── assets/
│   ├── asset-manifest.json
│   ├── backgrounds/
│   ├── characters/
│   ├── enemies/
│   ├── sprites/
│   ├── ui/
│   ├── props/
│   └── effects/
└── .playtest/
    ├── inputs/
    │   ├── interaction-contract.json
    │   ├── objectives.json
    │   ├── rubric.json
    │   └── layout-contract.json
    └── tests/
        ├── manifest.json
        └── *.test.mjs
```

Level DSL files are produced by `level-design-agent` under `levels/`, not by
Scaffold.

## Minimal runtime files

### `package.json`

Declare ESM and keep scripts limited to tests actually used by the pipeline.
The verified game server is static and performs no build step; browser modules
must not depend on bare package imports.

### `index.html`

Provide `#game-container`, one classic `phaser@4` CDN script, then
`src/main.js` as a module. The CDN script must load first and expose the single
runtime as `globalThis.Phaser`. Do not embed game logic, load remote stylesheets,
load a second Phaser runtime, or use bare `import ... from 'phaser'` specifiers;
they do not resolve under the static game server.

### `src/main.js`

Consume `globalThis.Phaser`, import runtime config and every scene declared by
the GDD §1 scene registry, then build the Phaser config from projected values;
do not invent genre defaults here.

### `src/config.js`

Project authored constants from the GDD and, when applicable, Level DSL:

```js
export const CONFIG = Object.freeze({
  screen: { width: /* GDD canvas width */, height: /* GDD canvas height */ },
  physics: /* GDD §0.2 physics contract */,
  input: /* GDD §0.2 action bindings */,
  ui: /* GDD §2E authored UI dimensions */,
});
```

No gravity, movement speed, cooldown, key binding, or combat value is a template
default. If an authored value is missing or contradictory, return a design
change request.

### `src/scenes/<SceneKey>.js`

Create one parseable Phaser scene stub per GDD §1 scene key. Stubs may declare
`preload`, `create`, and `update`, but must not implement gameplay or add
untracked placeholder visuals.

## Canonical runtime helpers

Copy the relevant helper byte-for-byte:

| Condition | Source | Destination |
|---|---|---|
| Every game | dispatched `DISPLAY_UTILS_PATH` | `src/display-utils.js` |
| GDD declares particle VFX | `../implement/particle_recipes.js` | `src/utils/particle-recipes.js` |
| Side-view platform controller is required | `../implement/character-controller.js` | `src/utils/character-controller.js` |

Do not paste helper bodies into this document or adapt them during Scaffold.

## Projected contracts

| Output | Canonical schema | Example |
|---|---|---|
| `assets/asset-manifest.json` | `schemas/asset-manifest.schema.json` | `examples/manifest/` |
| `.playtest/inputs/interaction-contract.json` | `schemas/interaction-contract.schema.json` | `examples/contracts/*-interaction.json` |
| `.playtest/inputs/objectives.json` | `schemas/objectives.schema.json` | `examples/contracts/objectives.json` |
| `.playtest/inputs/layout-contract.json` | `schemas/layout-contract.schema.json` | `examples/contracts/layout-cluster.json` |

### Cross-contract invariants

Individual schemas cannot express these relationships. Preserve them during
projection; `scaffold_done` and downstream coverage gates enforce them:

- Copy `genre` and `interaction_profile` exactly from the GDD's immutable
  Startup routing metadata. The interaction contract must equal the values
  persisted in `.playtest/state.json`; do not normalize or re-route them.
- Manifest `runtimeSources` is the duplicate-free, order-insensitive set of
  every runtime `sourceStrategy` used by layout elements and nested children.
  Do not add unused sources or omit used ones.
- Every file-backed layout node, including nested children, has a `key` with a
  manifest row whose `sourceStrategy` matches. This is one-way coverage:
  manifest rows that are not layout nodes are allowed.
- A waived scene remains declared under `scenes`, appears exactly once in
  `waived_scenes`, and uses exactly `waived:pure-transition` or
  `waived:concept-generation-unavailable` as its `reference`. See
  `examples/contracts/layout-waived-scene.json`.
- Objective IDs and interaction layer names are unique. Manifest logical keys
  and all effective Phaser texture keys, including sprite action texture keys,
  are unique.
- A `key` or `bind` identity is not reused by different source-backed layout
  nodes within one scene. Across scenes, the same identity may be reused only
  with the same `sourceStrategy`.

### Asset manifest projection

- Use schema v2 and one row per logical entity or static asset.
- Sprite actions belong under that entity's `actions` map. The top-level
  `path`/`metadata` mirror `defaultAction`.
- Preserve each GDD registry row's `sourceStrategy`, description, authored
  dimensions, anchors, slots, and value-less/binding constraints.
- `generated-image`, `supplied-image`, and `licensed-image` are file entries.
  Runtime-only strategies are declared in `runtimeSources`, not fake asset rows.
- `responsive`, `pixel_art`, and `minimal` may use the no-file contract only when
  every visual source in the layout contract is runtime-produced.

### Interaction projection

Translate GDD §0.2 literally:

- `actions` maps semantic actions to executable input primitives.
- `entities` maps logical entity keys to layer, body, shape, and bbox-fit policy.
- `matrix` is the explicit layer-pair interaction topology.
- `world` contains only authored world/physics settings.

Physics and pointer games require non-empty layers and matrix rows. Board or
text games may use empty topology only when they have no physical entities or
pointer actions.

### Layout role and UI sizing projection

Every layout element projects the GDD §2.0.2 `role` and `sourceStrategy`; both
are required by the layout schema. UI sizing remains profile-specific:
`art_directed` projects exactly one native `targetWidth` or `targetHeight`,
responsive/minimal project `logicalBox` or `maxBox`, and pixel art projects
`baseSize` plus non-empty positive `integerScales`. Asset Integration maps those
fields to the runtime helper contract; Scaffold does not collapse them into a
generic width/height pair.

### Objectives, rubric, and tests

- `objectives.json` must include `start_game` and the prompt's essential
  mechanics. IDs are unique lowercase filesystem-safe slugs; executable string
  fields must contain a non-whitespace character. See
  `examples/contracts/objectives.json`.
- The objective DSL is closed, not descriptive:
  - `setup` accepts only `scene`, `stop`, `data`, `state`, `snapshot`, `eval`,
    and `js`; `stop`/`data` are legal only when `scene` is present;
  - `success` is exactly one of
    `{activeScene}`, `{scene, custom, op, value}`, or
    `{scene, group, field, op, value}`;
  - operators are `>`, `>=`, `<`, `<=`, `==`, or `!=`.
  Unknown keys fail contract validation before a browser or model is launched.
  Do not emit prose placeholders such as `seedBoard`, `read`, or `predicate`.
  If one predicate cannot represent a compound acceptance rule, split it into
  focused objectives and cover the conjunction in a deterministic test.
- Project the acceptance rubric from the GDD; see `../verify/rubric-guide.md`.
- Generate deterministic tests and `.playtest/tests/manifest.json`; see
  `../verify/test-patterns.md`.

## Reprojection

In `MODE=reproject`, update only outputs affected by `CHANGED` or `ISSUES`.
Preserve producer-owned fields and sidecars for unchanged manifest entries.
When an owned file becomes obsolete, report its path in `stale_owned_paths`;
write that exact list to `.playtest/inputs/scaffold-reproject.json`, seal the
fixed declaration with `mcp__gca__seal_scaffold_reproject({})` after all writes,
and return its `receipt_id`. Every declaration uses a fresh
`reproject-<32 lowercase hex>` ID; replaying an ID is rejected. The orchestrator consumes that receipt with
`mcp__gca__delete_scaffold_owned_paths` before rerunning the Scaffold gate.
