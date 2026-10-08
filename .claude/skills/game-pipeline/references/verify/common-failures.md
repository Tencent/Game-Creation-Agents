# Common Failure Catalog

A living document. Append new patterns as they are
discovered and verified. When the play-agent confirms a bug and a fix is verified
by a re-run, append the signature → root cause → verified fix here so the loop
accumulates repair knowledge across games.

Composite membership is machine-owned and queryable through
`gate_check({ gate: "gate_contract" })`; the table in `verify-agent.md` is only
an advisory gate-to-evidence routing index.

## Gameplay / Logic Failures (found by the play-agent)

These are not crashes — the game runs but an objective is unachievable or a
mechanic is incoherent. Screenshots/smoke tests pass; the play-agent catches them
by trying objectives and judging YES/NO/PARTIAL.

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| Objective stuck at start; score/progress never changes despite valid play | Win/progress condition unreachable from the initial state (e.g. eat requires `enemy < player` but player starts at the smallest size, so nothing is ever eatable) | Make the starting state actionable — spawn at least one valid target at start (e.g. allow same-size eat, or guarantee a smaller-category spawn), or start the player one size up |
| play-agent reports `frozen_state` | At least 6 consecutive compact engine-state signatures are identical. The signature includes object count, group active/total counts, and primitive custom fields—not positions or animation frames. Causes include a no-op/unsupported action, pause, unmet predicate, intentionally static state, or a stopped loop | Check action resolution, pause/resume, objective predicate, and whether the contracted state should change. Inspect loop progress before changing `update()`; this oracle alone does not prove the loop stopped or independently fail `gatePlayQuality`. |
| play-agent reports `inert_scene` | With at least 4 captured frames, visual diversity is `<= 0.25`. A static-by-design UI may trigger it while logic progresses; animation can also hide stuck logic | Inspect frame hashes separately from engine state and confirm the intended feedback. Add motion/feedback only when product intent requires it; do not merge this diagnosis with `frozen_state`. |

## Scene Failures

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| "Scene 'X' not found" | Scene not imported or not in scene array | Import class in main.js AND add to scene array |
| Black screen, no errors | Entry/transition flow, preload, or active-scene content is wrong; the array index alone is not authoritative | Confirm the design-specified entry scene is registered and first, its preload completes, and `create()` calls the expected transition (for example `this.scene.start("TitleScene")`). A common flow is `BootScene` → `TitleScene`, but use the game's real scene keys. Do not blindly move `TitleScene` to index 0; run the formal smoke test and require the expected scene to become active with an empty `window.__gameVerifyErrors`. |
| Scene starts but is empty | create() not implemented or has early return | Check create() method body |
| "Cannot read properties of undefined (reading 'start')" | Calling scene.start on wrong context | Use `this.scene.start()` inside a Scene method |

## Physics Failures

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| "Cannot read properties of null (reading 'setVelocityX')" | Game object has no physics body | Call `this.physics.add.existing(obj)` first |
| Objects fall through floor | No collider registered | Add `this.physics.add.collider(player, ground)` in create() |
| Objects jitter or teleport | Collider registered in update() (runs every frame) | Move collider registration to create() |
| Player stuck in walls | Body too large for gaps | Use `body.setSize(w, h)` and `body.setOffset(x, y)` |
| Player doesn't jump | jumpForce is positive (should be negative for upward) | Set jumpForce to negative value, e.g. -400 |
| Objects float (no gravity) | Physics gravity is 0 | Set `gravity: { y: 300 }` in game config |

## Input Failures

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| Keys don't respond | cursors created in update() instead of create() | Move `createCursorKeys()` to create() |
| Click doesn't register | Object not interactive | Call `object.setInteractive()` before adding listeners |
| Event fires multiple times | Using `on` instead of `once` for one-shot events | Use `once` for start/restart triggers |
| "keyboard is null" | Accessing input before scene is ready | Use input only in create() or update() |

## Config Failures

NaN/Infinity symptoms are consolidated under **Silent State Corruption**;
a string-valued numeric config is one candidate cause, not a separate failure
record.

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| "Cannot read properties of undefined" on CONFIG.x.y | Missing nested key in config | Add the key path to config.js |
| Import error on config.js | Missing `export` keyword | Use `export const CONFIG = { ... }` |

## Asset Failures

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| "Texture 'X' not found" | Key mismatch or image not loaded | Verify key matches between load() and add.sprite() |
| Image not showing | Loaded in create() instead of preload() | Move this.load.image() to preload() |
| CORS error loading image / ES module failure from `file://` | Game was opened without the pipeline HTTP server | Start only with `python3 <REPO_ROOT>/scripts/serve_game.py <GAME_DIR> --base 3000`, capture its exact `URL=http://127.0.0.1:<port>`, and pass it to `run_verify_dag`. Stop with the same script's `--stop`; never guess a port or bypass URL identity. |

## Game Loop Failures

Growth/leak and restart-state symptoms are consolidated under **Silent State
Corruption** below.

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| Game freezes | Infinite loop in update logic | Check while-loops, recursive calls for termination |
| Timer doesn't fire | Using setTimeout instead of Phaser timer | Use `this.time.addEvent()` or `this.time.delayedCall()` |
| Score shows on wrong scene | scoreText not using setScrollFactor(0) | Add `.setScrollFactor(0)` for HUD elements |

---

## Pipeline Gate Failures (signature → authority)

This table routes common signatures to their evidence owner. It does not
redefine gate logic; query
`mcp__gca__gate_check({ gate: "gate_contract" })` for current composite
membership and use `verify-agent.md` for sequencing.

| Error Signature | Likely Cause | Authoritative Route |
|----------------|--------------|---------------------|
| `VERIFY_URL_MISMATCH` | Producer URL differs from the URL bound to the verify epoch | Restart through `serve_game.py` when needed and call `run_verify_dag` with the exact served URL; recovery reuses the active URL/ID verbatim |
| `VERIFY_RUN_FINALIZED` | A producer tried to append evidence after `write_verify_report` | Start a fresh verify epoch; never append to the finalized run |
| `<label> runtime_identity is stale/cross-run` | Producer evidence belongs to another epoch/build or predates current inputs | Open a fresh epoch and rerun that producer with exact `RUN.url` and `RUN.verify_run_id`; never copy or restamp old evidence |
| Runtime asset receipt missing, stale, cross-run, or missing reachable use | Current token-bound `loaded_texture_keys`, `animation_frame_texture_keys`, `display_list_texture_keys`, or `reachable_scene_keys` evidence is absent/incomplete | Run `mcp__gca__capture_runtime_assets({ game_dir: GAME_DIR, url: RUN.url, verify_run_id: RUN.verify_run_id })`; fix preload, animation frames, or reachable-scene display use rather than substituting source-string evidence |
| Build digest or `verify-report.json` is stale/cross-run | Evidence belongs to an earlier build or epoch | Refresh the build, open a fresh epoch, rerun required producers, then write a new report |
| `formal test-results.json missing` / `canonical test-results.json is not a full unfiltered .playtest/tests/ run` / `solo/scoped evidence cannot satisfy the release gate` | Only `scoped-test-results.json`/solo evidence exists, the canonical envelope is scoped, or tests were skipped | Run canonical `.playtest/tests/` with no filter/id/changed/solo selector and zero skips; never promote a scoped artifact |
| Slot receipt, subject margin, bbox/crop/aspect failure | Generated asset metadata or slot evidence disagrees with the projected contract | Route to `assets-agent`, which owns `mcp__gca__validate_asset_slot`, to refresh the exact manifest runtime path when slot evidence is stale; route margin/bbox/crop/aspect to their named asset checks and sidecars |
| UI art parity, binding, layout, or static-text failure | Runtime UI integration disagrees with the projected UI/display contract | Run `mcp__gca__check_ui_contract` for diagnosis, fix through the owning integration path, then rerun the composite gate; a whole-screen screenshot cannot waive it |
| `visual_diff` reports `status:"failed"` / `match:false` | A real current visual mismatch was measured | Fix art/integration/layout as routed and regenerate `visual_diff` evidence |
| `visual_diff` reports `status:"unavailable"` | The pinned Doubao request failed or timed out | Record the warning and retry later without counting a round; do not rewrite it as a pass. Missing/stale/error evidence and real mismatches remain blocking |
| Layout capture missing, stale, or misplaced | The Axis 5 capture/materialization chain is incomplete or older than a layout input | Follow **Layout Alignment Failures** below and the Axis 5 workflow in `verify-agent.md` |
| Interaction-fit, topology, body-space, tunnel, or prefab-coverage failure | Measured body/display/topology contract disagrees with runtime wiring | Run `mcp__gca__check_interaction_contract`, fix `display-params.json`/entity and prefab wiring through its owner, and retain the measured interaction gate |

---

*Append new patterns below this line as they are discovered:*

## Matter.js Body Lifecycle Failures

Generic tunneling is consolidated under **Physics Containment Failures**.
This section keeps only the distinct Matter lifecycle/scale signature.

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| `Cannot read properties of undefined (reading 'position')` inside `MatterImage.preUpdate` | A coupled display/body was destroyed mid-step and the display still attempts to synchronize from the body | For frequently destroyed/merged objects, prefer a decoupled image plus Matter body, synchronize deliberately, and clean both up atomically. `matter.add.image()` remains valid for static/non-destroyable objects; choose from measured lifecycle evidence rather than banning the API globally. |
| Game freezes after many merges/destructions with no visible error | A prior mid-step body/display failure aborted the step; a gameplay unlock waiting on the clock may never run | Use timestamp/state-machine cooldowns for gameplay-critical ability gates. Phaser timers remain valid for non-critical cleanup/VFX when their scene shutdown lifecycle is handled. |
| `setScale(0.01)` pop-in causes tunneling or zombie bodies | Tweening scale on a coupled Matter object also collapses its collision body | Use a decoupled body for independent visual animation, or animate a verified visual-only representation. Re-check body size/offset against the interaction contract; do not assume a display setter is physics-neutral for every object type. Then follow the measured tunneling route under **Physics Containment Failures**. |

## Phaser v4 Migration Failures

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| `generateTexture is not a function` | Wrong receiver or Phaser build/version; this does not prove that Graphics removed the method | Record the failing receiver and `Phaser.VERSION`. Phaser 4 removed `Create.GenerateTexture` and `TextureManager.generate`, while the repository's supported Graphics reference still uses `gfx.generateTexture(key,w,h)`. Fix receiver/import/build context first; use the migration path only for the removed APIs. |
| `Phaser.Display.Masks.BitmapMask is not a constructor` | v4 replaced BitmapMask with Filters | Use `sprite.enableFilters(); sprite.filters.internal.addMask(maskObj);` |
| `setTintFill is not a function` | v4 removed `setTintFill()` | Use `sprite.setTint(color).setTintMode(Phaser.TintModes.FILL)` |
| `Phaser.Geom.Point is not a constructor` | v4 replaced Point with Vector2 | Use `new Phaser.Math.Vector2(x, y)` |
| Sprites blurry / sub-pixel jitter | v4 defaults `roundPixels` to `false` | Add `roundPixels: true` to game config |
| Canvas fallback not rendering filters | v4 Canvas renderer is deprecated, doesn't support filters | Use `type: Phaser.WEBGL` in game config |

## Body / Display Mismatch (Arcade and measured contracts)

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| Body ~2px wide despite display 48px+; overlaps miss; immovable sprites drift | `body.setSize(w,h)` uses texture/frame space while the measured bbox, scale, `bodySpace`, or offsets are missing/wrong | First measure and declare content bbox, scale, `bodySpace: frame|world`, body dimensions, and offsets in `.playtest/inputs/display-params.json`; use canonical `applyDisplay()` order: origin → `body.setSize(w,h,false)` → body offset → scale, then rerun the interaction-fit diagnostic/gate. Use Zone + separate sprite only when an independent hit region is intentional, keep positions and entity keys synchronized, and still satisfy the measured contract. |

## Physics Containment Failures

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| Objects slip through floor at corners | Wall and floor bodies share corner vertices but don't overlap — diagonal seam | Overlap all joints: walls extend below floor, floor extends wider than walls. Physics thickness ≥ 2× smallest body radius. |
| Small objects/bodies tunnel through thin walls or targets | Depending on `interaction_profile` and measured evidence: coupled Matter scaling shrank the body, body/wall topology is too thin, or per-frame travel meets/exceeds the target body | Start with `mcp__gca__check_interaction_contract`. For coupled Matter scale, rebuild the body or decouple display/body. For topology, enlarge the measured body/wall and overlap corner joints. For excessive travel, enlarge the projectile body on its travel axis, cap speed, or use overlap plus positional checking/a multi-step sweep; tune engine-specific iterations only when profile evidence warrants it. Re-run the measured interaction gate. |
| Objects escape world (position at ±Infinity) | No world bounds; object leaves container and accelerates forever | Always call `matter.world.setBounds()` as a safety net. |
| Spawned object appears inside a wall | Object created at midpoint of two colliding bodies, midpoint is inside wall geometry | Clamp spawn position to container interior: `x = Clamp(x, minX + radius, maxX - radius)`. |

## Silent State Corruption

Failures that may emit no JavaScript error and are most reliably found through
programmatic state/performance evidence.

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| Entity count or memory grows continuously; FPS degrades | Dead entities remain tracked, objects are created every frame, or display/body resources are not cleaned up together | Measure which collection/resource grows, remove dead entries, move reusable creation out of `update()`, and pool or destroy display/body pairs atomically. |
| NaN/Infinity appears in gameplay state (position, velocity, score, or calculation) | Candidate causes include reading a removed body, division by zero, or a string-valued numeric config | Trace the first non-finite value, correct its body lifecycle/denominator/config type, and explicitly assert `Number.isFinite(...)` on the affected invariant. Destroying an invalid entity is containment, not proof that the root cause is fixed. |
| State wrong after scene restart / persists across restarts | Variables not reset in `create()` | Initialize ALL mutable state in `create()`, never constructor |
| Ghost bodies (invisible collisions) | Sprite destroyed but body not removed, or vice versa | Always destroy both together in one function |
| Gradual drift (game "feels wrong") | Floating-point accumulation from frame-by-frame mutation | Store base values; compute from base each frame, never from current value |

Verify these repairs with deterministic formal invariant/restart tests following
`test-patterns.md`. `play_game`'s invariant oracle additionally reports
`nan_position`, `nan_velocity`, and threshold-based `entity_leak` findings as
runtime diagnostics; those findings do not replace the formal regression.

## Layout Alignment Failures (Axis 5 — lays-out-like)

Signature → fix routing for layout evidence. The canonical capture →
materialize → report workflow, freshness inputs, and gate authority live in
`.claude/agents/verify-agent.md` under **Axis 5 — Layout alignment** — do
not duplicate that sequence here.

| Error Signature | Root Cause | Verified Fix |
|----------------|-----------|-------------|
| `layout coverage X% < Y%` — element "never rendered (missing or unnamed)" | Element placed without a name (so the bounds dump can't find it), or never placed at all | Place it via the helpers (`addUiImage`/`placeUiText`/`applyDisplay` set `obj.name = <layoutKey>`); for a raw `scene.add.*`, set `obj.name = key` + `obj.setData('layoutKey', key)`. If the element has no §2.0.2 row at all → design-agent re-measures with `detect_layout` and adds the row → scaffold re-projects the contract. |
| `'<key>' is N% of the canvas diagonal off its §2.0.2 position` | Element placed with an eyeballed / hardcoded coordinate instead of the measured one | Derive position from `CONFIG.ui.<key>` `{x,y}` (which derives from §2.0.2); pass it to `addUiImage(scene, x, y, key, {origin})`. Don't invent coordinates — the §2.0.2 row is the source of truth. |
| `zone '<key>' centroid (...) is outside its §2.0.2 region` | A `kind:zone` group (enemy formation, card row, spawn field) is built in the wrong screen area | Anchor the group's layout origin / spawn rect to the §2.0.2 `box` for that zone (use its center/extent), not an arbitrary constant. |
| `'<key>'` spawn entity off its position (kind:spawn) | Entity boots at a hardcoded spawn instead of its §2.0.2 spawn point | Spawn the entity at the §2.0.2 row's center (recorded in `display-params.json` entity spawn positions); `applyDisplay` already names it for the dump. |
| `scene '<X>' not captured` / `no scene was captured` | The current epoch has no complete layout capture | Follow the Axis 5 workflow in `verify-agent.md`; `capture_layout` covers every scene in `layout-contract.json`. |
| `scene '<X>': rendered bounds are stale relative to layout inputs — rerun capture_layout` | Capture predates a change to `src/`, `assets/`, `design/`, `layout-contract.json`, or `gca.config.yaml` | Refresh the build digest, open a fresh epoch, and rerun the Axis 5 workflow in `verify-agent.md` before the composite gate. Never reuse old bounds after a non-`src/` layout-input change. |
| Element correct in art (Axis 3 passes) but flagged by Axis 5 | Right asset, wrong screen position — looks-like axes don't check coordinates | This is exactly what Axis 5 exists to catch; fix the placement coordinate (above), not the art. |
