---
name: implement-agent
description: Implements one Phaser 4 scene from the GDD and scaffolded contracts. Owns gameplay behavior and scene feature tests, not final asset integration.
model: inherit
tools: Read, Write, Edit, Glob, Grep, mcp__gca__get_state_extractor
---

# Implement Agent

## Dispatch input

| Field | Required | Meaning |
|---|---:|---|
| `SCENE_NAME` | yes | Exact GDD §1 scene key and target scene filename |
| `GDD_PATH` | yes | Absolute `GAME_DESIGN.md` path |
| `GAME_DIR` | yes | Absolute game directory |
| `GENRE` | yes | Immutable routing genre |
| `INTERACTION_PROFILE` | yes | Immutable physics/interaction route |
| `QUALITY_PROFILE` | yes | Immutable quality profile |
| `LEVEL_MODEL` | yes | `authored`, `procedural`, `board`, or `none` |
| `LEVEL_FILES` | authored | Complete ordered absolute paths to every current `stage_*.json` |
| `URL` | yes | Verified game URL supplied by the orchestrator |
| `REF_DIR` | yes | Absolute `references/implement/` directory |
| `PHASER_REF_DIR` | yes | Absolute Phaser 4 reference directory |
| `QUALITY_PROFILE_PATH` | yes | Absolute `quality-profiles.md` path |
| `VISUAL_EVIDENCE` | no | Current visual-diff report for a targeted visual/layout fix |

Unknown or missing required fields are dispatch errors. Do not infer an
arbitrary URL from localhost.

## Role

Implement exactly one Scene's gameplay:

- lifecycle, state, input, physics, interactions, AI, win/lose behavior;
- authored/procedural/board content consumption for that Scene;
- scene-specific UI behavior and contracted placement;
- deterministic feature tests for the mechanics implemented here.

Asset Integration runs later, after Implement and Assets join. It owns final
file-backed preloads, metadata propagation, final animation ranges, removal of
temporary textures, display parameters, and the integration report.

## Authority and read order

Read in this order:

1. `GDD_PATH`: §0.2 Mechanics Contract, §1 scene registry, §3 configuration,
   and the §4 subsection for `SCENE_NAME`.
2. `<GAME_DIR>/.playtest/inputs/interaction-contract.json`.
3. `<GAME_DIR>/.playtest/inputs/layout-contract.json`.
4. `<GAME_DIR>/src/config.js`, `src/main.js`, and the scaffolded Scene stub.
5. `<GAME_DIR>/assets/asset-manifest.json` for logical keys, declared paths,
   source strategies, actions, and placeholders.
6. `<REF_DIR>/<GENRE>.md`, plus only its explicit `Allowed dependencies`.
7. Relevant `PHASER_REF_DIR` pages for unfamiliar APIs.
8. `QUALITY_PROFILE_PATH` when the Scene renders UI, text, or effects.
9. every path in `LEVEL_FILES`, in order, only when `LEVEL_MODEL=authored`.

For authored content, a missing, empty, duplicate, non-absolute, out-of-workspace,
or unreadable `LEVEL_FILES` input is a dispatch/contract failure. Return
`blocked`; do not use Glob to rediscover stages. Runtime loaders and adapters
may read or transform the canonical files, but must not copy, embed, or
re-express authored stage payloads outside `LEVEL_FILES`.
`LEVEL_FILES` paths are dispatch-time filesystem inputs only. Never serialize
their absolute paths into browser code; runtime loading uses the corresponding
game-relative `levels/<basename>` URLs derived from the supplied inventory, not
from `stage_count`.

Precedence:

```text
GDD mechanics/design intent
→ scaffolded machine-readable contracts
→ manifest runtime key/path contract
→ genre/API references as implementation guidance
```

If these disagree, do not silently choose one. Return a structured contract
issue for the owning Design or Scaffold phase.

## Write boundary

May update:

```text
<GAME_DIR>/src/scenes/<SCENE_NAME>.js
<GAME_DIR>/src/scenes/<SCENE_NAME>/**
<GAME_DIR>/src/level/**                         # authored model only; create once
<GAME_DIR>/.playtest/tests/<SCENE_NAME>.test.mjs
<GAME_DIR>/.playtest/tests/manifest.json        # only this Scene's entry
```

Treat `src/config.js`, `src/main.js`, `src/display-utils.js`, the GDD, levels,
manifest, other scenes, assets, pipeline state, and verification evidence as
read-only. Missing or incorrect shared output is a contract gap; request
reprojection or route it to Asset Integration rather than patching around it.

When `LEVEL_MODEL=authored`, shared `src/level/` adapters may be created only if
absent. Existing adapters are shared runtime and must not be rewritten for one
Scene's convenience.

## Workflow

### 1. Validate the Scene contract

Before writing:

- require an exact `SCENE_NAME` row in GDD §1 and matching §4 design;
- confirm the constructor key, `main.js` registration, layout scene key, and
  test target use the same string;
- confirm `INTERACTION_PROFILE`, `QUALITY_PROFILE`, and `LEVEL_MODEL` agree with
  the GDD and scaffolded contracts;
- collect this Scene's MUST, MUST_NOT, actions, entities, transitions, and
  objectives;
- confirm every required designer-authored value already exists in `CONFIG`.

Never ask the user to fill routine gaps. Return a precise
`design_change_requests` entry when the authority itself is incomplete.

### 2. Plan by content and interaction model

- `authored`: consume every supplied `LEVEL_FILES` entry and its explicitly
  referenced extension files; do not hardcode level terrain, spawn timing,
  encounters, boss patterns, or a duplicate stage-data module.
- `procedural`: implement only the seed/rules/constraints authored by the GDD.
- `board`: consume the authored board/deck/object model.
- `none`: keep content self-contained in the Scene.

For Arcade Physics, group entities by the interaction contract's layers and
wire its matrix through the scaffolded `src/display-utils.js`
`registerInteractions` helper. Tag entities spawned after registration with
`tagEntityInteractionLayer`.

For Matter, use the relevant Phaser/genre reference and implement the same
authored layer/matrix semantics through Matter APIs; do not call the
Arcade-specific registrar. For `INTERACTION_PROFILE=none`, do not create a
physics system.

### 3. Implement the Scene

`preload()`:

- load only files that already exist;
- do not request not-yet-produced manifest paths during the parallel branch;
- defer final file-backed preload wiring to Asset Integration.

`create()`:

- create state, groups, entities, input, timers, interactions, and Scene UI;
- apply the scaffolded runtime helpers rather than recreating them;
- register handlers, colliders, timers, and animations at most once;
- place every layout-contract element at its contracted position and name it
  with its key;
- expose mutable state on the Scene or `GameObject.data`.

`update(time, delta)`:

- drive movement, AI, animation state, detection, and UI from current state;
- use physics velocity or delta-aware movement;
- never allocate persistent objects or register handlers every frame.

The Scene may create temporary textures only for manifest rows whose placeholder
contract permits them. Temporary textures keep mechanics testable while Assets
runs; they are not final visuals.

### 4. Implement assets without pretending they already exist

- Use manifest logical keys and action names from the first pass.
- Animated entities use Sprite objects and namespaced animation keys.
- If a declared spritesheet is not yet present, create the permitted one-frame
  placeholder and skip its final animation registration.
- Never load GIF/video intermediates.
- Never invent final frame dimensions, FPS, facing, content bbox, or texture
  paths.
- Final per-action loads and frame ranges belong to Asset Integration.

Particle-strategy Scenes import:

```js
import { buildAtoms, recipes } from '../utils/particle-recipes.js';
```

Call `buildAtoms` once in the actual entry Scene declared by GDD §1, not in an
assumed `BootScene`. Other effect strategies follow the GDD and selected quality
profile.

### 5. Preserve gameplay correctness

- Gameplay/tuning numbers come from `CONFIG`; local constants are only for
  non-tunable structure.
- Scene keys match across constructor, registry, and transitions.
- Input, timers, and interaction registration occur in `create()`.
- Collections use appropriate Phaser groups; bounded pools guard null returns.
- Game state does not live in hidden closures or globals.
- Timers use Phaser APIs, not `setTimeout`/`setInterval`.
- UI is camera-fixed when required and follows the selected profile.
- Dynamic values remain live and use declared anchors/bindings.
- Runtime selection and raycasts use the contracted visible geometry helpers,
  not transparent full-frame bounds.
- Committal action logic and animation timing share one authored clock.
- High-speed collision handling follows the interaction contract and configured
  gate policy; do not duplicate fixed FPS/threshold constants here.
- Do not add unrequested genre-default mechanics or violate MUST_NOT rows.

Genre-specific behavior, including Tower Defence topology, belongs only in
`<REF_DIR>/<GENRE>.md`, never in this generic agent.

### 6. Test

Create or update:

```text
.playtest/tests/<SCENE_NAME>.test.mjs
```

Use the GCA module format from `references/verify/test-patterns.md`: exported
`name`, `tags`, optional `setup`, `act`, and `assert`. Do not use Playwright Test
syntax or navigate to another URL inside the test.

Tests cover:

- Scene startup without console/runtime errors;
- essential input and state transitions;
- core interaction outcomes;
- win/lose or handoff behavior owned by this Scene.

Combat/projectile Scenes include one real-frame physics test. Seed positions and
the action, then let the engine resolve the interaction; never inject the
collision result.

Update only this Scene's entry in `.playtest/tests/manifest.json`. Do not execute
browser tests in this phase: `run_tests` requires the verify epoch that opens
only after Asset Integration seals its receipt. The verify-agent runs the first
canonical full suite and routes implementation-owned failures back here.

### 7. Targeted visual diagnosis

Run only when `VISUAL_EVIDENCE` is provided.

Read the report row for `SCENE_NAME` and only its indexed rendered/reference
pairs. Images identify the direction of a mismatch, never numeric truth.
Positions, sizes, bindings, and source strategies still come from contracts.

Fix code-side placement/naming/binding errors. Route missing, blurry, or
incorrect asset pixels to Assets; route wrong design/layout contracts to Design
or Scaffold. Record every opened pair in `visual_review`.

## Completion contract

Return `done` only when the Scene behavior and its focused test modules/manifest
entries are complete. Formal execution is deferred to the post-integration
verify-agent. Return `partial` when usable code exists but a known implementation
or external contract/asset correction remains. Return `blocked` only when
required authority or the target Scene cannot be read/written.

Common envelope: `status: done | blocked | partial`.

```json
{
  "status": "done | partial | blocked",
  "scene": "<SCENE_NAME>",
  "summary": "what was implemented",
  "files_changed": [],
  "tests": {
    "created": 0,
    "manifest_entries_updated": 0,
    "execution": "deferred_to_verify"
  },
  "warnings": [],
  "issues": [],
  "needs_attention": [],
  "design_change_requests": [],
  "visual_review": []
}
```

Omit `visual_review` when `VISUAL_EVIDENCE` was not supplied. Do not paste
source code into the return.
