# Terrain Prefabs — collision shapes + standard build algorithms

`terrain[].chunk` in a level file is one of a **canonical vocabulary** (enum in `level_schema.json`). Each id maps to a defined collision shape that `implement-agent` must build, and `gatePrefabCoverage` (platform incl. auto-scroll variant / authored beat_em_up) blocks any chunk used in `levels/*.json` that has no builder in `src/`. This file is the SSOT for what each chunk means and how to build it — author levels from this vocabulary, implement builders to match.

The builder convention the gate looks for (any one): a `buildChunk_<id>(...)` function, a `case '<id>':` in a chunk switch, or a `<id>:` key in a terrain `REGISTRY`. Keep all chunk builders in one place (e.g. `src/level/TerrainAssembler.js`).

## Collision classes

| Class | Verb | Arcade body | Chunks |
|---|---|---|---|
| **Solid** | `collide` | static, full AABB | `flat_ground`, `raised_ground`, `high_platform`, `floating_platform`, `wall`, `ceiling`, `bridge`, `boss_arena` |
| **One-way** | `collide_oneway` | static, top-only | `oneway_platform` |
| **Slope** | `collide` | static AABB stair steps, or an explicit ground post-process | `ramp_up`, `ramp_down` |
| **Gap** | — (no body) | none — empty space | `pit`, `gap`, `trench` |
| **Kinematic** | `collide` + carry | immovable + velocity | `moving_platform` |
| **Sensor / swim** | `overlap` | sensor (not solid) | `water` |
| **Partial** | `collide` + gap | solid spans + a gap | `broken_bridge` |
| **Marker** | `overlap` | sensor | `start_pad`, `goal` |

The interaction-contract layers map onto these: solid/slope/kinematic/partial → `terrain`; one-way → `oneway`; `water`/`start_pad`/`goal` are sensors the matrix wires with `overlap`.

## Standard build algorithms (Phaser 4 Arcade)

All authored `x/y` values identify the chunk's top-left corner; Phaser GameObjects may convert that to a center internally. `width` must be positive. `height` must be positive for every body/sensor chunk and is omitted only for `pit`/`gap`/`trench`. If `y` is omitted, the assembler must resolve the GDD-authored ground level before dispatching the builder. Builders receive `{ x, y, width, height, params }`, default `params` to `{}`, and add bodies only to the matching interaction group. Entity bodies still come from measured `display-params`; terrain sizes come from the level data.

### Solid (flat_ground, raised_ground, high_platform, floating_platform, wall, ceiling, bridge, boss_arena)
A static axis-aligned rectangle. `wall` and `ceiling` stay axis-aligned: express their orientation through width/height and placement, not GameObject rotation (Arcade bodies do not become rotated polygons).

```js
buildChunk_flat_ground(x, y, w, h) {
  const g = this.add.rectangle(x + w / 2, y + h / 2, w, h, 0x000000, 0); // visual is the tilesprite; body is invisible
  this.physics.add.existing(g, true);                                    // true = static body
  this.groups.terrain.add(g);
  return g;
}
```

### One-way platform (`oneway_platform`)
A static body that only blocks a body falling onto its top. Use `oneWayProcess` (from `display-utils.js`) as the collider's process callback — never `checkCollision` hacks per-frame:

```js
buildChunk_oneway_platform(x, y, w, h) {
  const plat = this.add.rectangle(x + w / 2, y + h / 2, w, h, 0x000000, 0);
  this.physics.add.existing(plat, true);
  this.groups.oneway.add(plat); // one-way only; never also add to groups.terrain
  return plat;
}
// wired centrally by registerInteractions: physics.add.collider(player, oneway, null, oneWayProcess)
```

`oneWayProcess` returns true (resolve) only when the actor is moving down and its pre-step bottom was at/above the platform top — robust to fast falls. When the controller reports `state.wantsDrop`, set `actor.body.gcaOneWayDropUntil` to the end of the short drop window as shown in `character-controller.md`; the process callback suppresses only the one-way collider until that timestamp. Never toggle `body.checkCollision.down`, because that body-wide switch also disables landing on ordinary solid terrain.

### Slope (`ramp_up`, `ramp_down`)
Arcade has no true sloped bodies. Two correct options:

1. **Stair-step** (simplest, deterministic): build N small solid AABBs approximating the slope. `ramp_up` rises from left to right; `ramp_down` mirrors it.
2. **Ground post-process** (smooth): treat `x/y/width/height` as the ramp bounding box and align the grounded actor body's bottom to the surface. With `t = clamp((actorCenterX - x) / width, 0, 1)`, `ramp_up` uses `surfaceY = y + height * (1 - t)` and `ramp_down` uses `surfaceY = y + height * t`. If the GDD defines a maximum slope, compare it with `atan2(height, width)` in degrees before accepting the chunk.

```js
buildChunk_ramp_up(x, y, w, h) {            // stair-step variant
  const steps = Math.ceil(w / 16);
  for (let i = 0; i < steps; i++) {
    const sw = w / steps, sh = h * ((i + 1) / steps);
    this.buildChunk_flat_ground(x + i * sw, y + h - sh, sw, sh);
  }
}
```

### Gap (`pit`, `gap`, `trench`)
**Create no body** — a gap is empty space. It still needs a coverage-visible no-op handler so the assembler and `gatePrefabCoverage` agree, for example `buildChunk_gap() { return null; }` (and equivalent handlers for `pit` and `trench`). A `pit` may carry a kill-sensor at the bottom (`params.kill: true` → an `overlap` sensor that triggers the lose outcome).

### Moving platform (`moving_platform`)
A **kinematic** body: `immovable = true`, `body.allowGravity = false`, moved by `setVelocity` or a back-and-forth tween. Riders standing on it must be CARRIED (`carryRider` from `character-controller.js`) or they slide off. Resolve push/squish per `character-controller.md`.

```js
buildChunk_moving_platform(x, y, w, h, params = {}) {
  const p = this.add.rectangle(x + w / 2, y + h / 2, w, h, 0x888888);
  this.physics.add.existing(p);
  p.body.setImmovable(true); p.body.setAllowGravity(false);
  p.body.setDirectControl(true); // required when a tween changes the GameObject position
  this.tweens.add({ targets: p, x: p.x + (params.range ?? 160), duration: params.duration ?? 2000,
                    yoyo: true, repeat: -1 });
  this.groups.terrain.add(p);
  return p;
}
```

### Water (`water`)
A **sensor** (overlap, not solid): wire `player ↔ water` as `overlap` in the matrix. On overlap apply the swim/drown rule from GDD §0.2 (buoyancy, slowed movement, or a timed drown). Never make water a solid body.

### broken_bridge
Solid spans with a gap in the middle: build two `flat_ground` solids separated by a `gap`. `params.gap_at` / `params.gap_width` place the hole.

### Markers (`start_pad`, `goal`)
Sensors used for spawn/teleport and win detection: `start_pad` positions the player at level load; `goal` is an `overlap` sensor that fires the `reach_end` win condition.

## Authoring rules

- Use ONLY vocabulary chunks; if you genuinely need a new shape, add it to the `level_schema.json` enum, document it here, and give it a builder (all three) — an unbuilt chunk renders as invisible/holey ground and `gatePrefabCoverage` will block it.
- A `pit`/`gap` between two solids needs no body, but the solids on each side DO — author both.
- Terrain geometry is authored (level data); entity bodies are measured (`display-params`). Never put pixel body sizes for the player/enemies here.
