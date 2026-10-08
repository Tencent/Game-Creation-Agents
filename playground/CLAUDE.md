# Generated Phaser Game Instructions

These rules apply to every generated game under `playground/<game-slug>/`.

## Runtime Contract

- Use Phaser 4 with JavaScript ES modules. Expose the game as
  `window.__phaserGame` for state extraction and tests.
- Ensure `globalThis.Phaser` is available before the game module graph runs.
  All generated modules consume that shared runtime and must not load or import
  another Phaser instance.
- Scene keys must match across constructors, the `main.js` scene list, and every
  `scene.start()` call.
- Register input handlers, colliders, groups, and timers in `create()`. In
  `update()`, drive movement, AI, animation, detection, and UI from current state;
  do not create persistent Phaser objects or register handlers every frame.
- Use Phaser groups for entity collections and Phaser timers instead of
  `setTimeout`. Keep mutable game state on scenes or `GameObject.data`, not hidden
  closure variables.

## Sources of Truth

- Designer-authored gameplay/tuning values and stable scene positions belong in
  `src/config.js` under `CONFIG`.
- When `level_model` is `authored`, per-level terrain, spawn timing, encounters,
  and boss patterns belong in `levels/*.json`, not scene source. Procedural,
  board, and no-level games do not fabricate this directory.
- Runtime asset keys, paths, frame dimensions, content bounds, and shipped UI
  dimensions come from `assets/asset-manifest.json` and metadata sidecars; the
  authored UI `Target size`/`Lock` originates in GDD §2E.
- UI sizing follows `quality_profile`: `art_directed` renders shipped UI 1:1;
  responsive/minimal profiles may uniformly fit at runtime; pixel art uses
  integer uniform scale. Non-uniform distortion is always forbidden.
- Layout positions derive from the layout contract; collision geometry derives
  from content-bbox/interaction metadata. Local structural constants may remain
  in code when they are not tunable design values.

## Assets and Tests

- Placeholder runtime textures are temporary and must be removed. Deliberate
  procedural art is allowed only when the selected profile/GDD declares it as a
  final asset strategy.
- Every final manifest asset/action must be consumed by source directly or through
  a manifest-driven runtime loader; review-only generation intermediates are not
  runtime assets.
- Static labels follow the quality profile: art-directed lettering ships as art;
  responsive/localized and minimal interfaces may use live text. Dynamic values
  always stay runtime-bound.
- Write smoke tests after scaffold, feature tests for implemented mechanics, and a
  regression test for every bug fix.
- Tests seed preconditions with `page.evaluate`; do not replay the whole game merely
  to reach a deep state.
- Use the relevant Phaser references under
  `.claude/skills/game-pipeline/references/phaser/` before implementing an unfamiliar
  subsystem.
