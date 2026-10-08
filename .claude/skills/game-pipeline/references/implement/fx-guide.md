# Particles FX — runtime-assembled visual effects

> **Reference doc** for game-pipeline sub-agents (assets / implement / scaffold / design).
> Applies when `QUALITY_PROFILE` + GDD select the particle-recipe strategy
> (`art_directed` defaults to it). Responsive/pixel/minimal may instead declare
> masks, shaders, deliberate flipbooks, or no effect; those strategies do not
> use the particle-specific sections below.

> **Decision rule — route by responsibility/role.** Visible entity/projectile
> bodies use the validated sprite-body path. Trails/impacts are separate effects;
> when particles are selected they use these recipes, while other profiles use
> their declared effect strategy.
>
> Never conflate an identity-bearing body with its effect. Particle-strategy
> games use particles for exploding/hitting/streaming roles.

For a selected particle strategy, assets-agent produces no FX/atom files and
implement calls these recipes. Universal rule: a projectile body is separate
from its trail/impact, regardless of how that effect is rendered.

---

## 0. Why particles, not i2v

i2v generates a 16-25 frame video per FX asset (~$0.80 per candidate × 4 candidates = ~$3.20), then RMBG-2.0 mattes every frame, often leaks the safe background color into tail frames, and produces a sprite-sheet that costs GPU memory and locks the FX to a single look.

Particles cost **$0** to produce (the 9 atom textures are drawn procedurally at boot via Canvas 2D), render at 60fps with hundreds of live particles, scale infinitely, parameterize trivially (palette, size, intensity), and never have a "tail-frame magenta" failure mode. Visual fidelity is excellent for the FX category because real-world effects *are* particle phenomena (smoke, sparks, shrapnel, light cones).

Earlier pipelines generated the 9 atoms via `generate_image(safe_bg=#FF00FF)` → `rmbg2_matting` → file. RMBG occasionally failed on low-contrast atoms (e.g. `atom_ring_shockwave`), leaving full-canvas #FF00FF rectangles that shipped to the game. Drawing the atoms procedurally eliminates that failure mode entirely — no API call, no matting, no file, no chance of magenta leakage.

Under the particle strategy there is no generated-FX exception. A profile/GDD
may instead declare a bounded flipbook, which is a separate file-backed strategy.

---

## 1. The atom inventory (9 runtime-built textures)

When particles are selected, Implement calls `buildAtoms(this)` once from the
`create()` method of the actual entry Scene declared by GDD §1. Scaffold only
copies the helper; there are no atom PNG files.

| Texture key | Size | Use cases |
|---|---|---|
| `atom_smoke_puff` | 128×128 | death explosion, big explosion, dissolve trail, grenade smoke |
| `atom_ember` | 64×64 | explosion sparks, projectile trails (fire), muzzle flash core |
| `atom_rock_chunk` | 64×64 | death explosion shrapnel, shield break debris |
| `atom_radial_flash` | 128×128 | muzzle flash, hit impact center, summon ring core, beam charge |
| `atom_ring_shockwave` | 128×128 | shockwave on death/big explosion, summon ring outer, shield break ring |
| `atom_dust_mote` | 32×32 | footstep dust, landing dust, slide dust |
| `atom_electric_spark` | 64×64 | electric projectile trail, electric hit, lightning strike, taser |
| `atom_slash_streak` | 96×32 | sword slash arc, claw scratch, crescent ultimate |
| `atom_glow_orb` | 96×96 | pickup glow, health glow, healing aura, charge-up tell |

**Atom rendering.** Each atom is drawn into a CanvasTexture via Canvas 2D API:
- Radial gradients (`createRadialGradient`) for soft alpha falloff atoms (`flash`, `ring`, `ember`, `glow`, `dust`, `smoke`).
- Polygon fills for `rock_chunk`.
- Stroke paths for `electric_spark` and `slash_streak`.

Atoms are mostly white/neutral so they recolor cleanly via `tint` at recipe time. `atom_ember` and `atom_glow_orb` ship with a warm baked color (because the gradient *is* the color story) but still accept `tint` overrides.

**To restyle an atom** (e.g. a pixel-art game wants chunkier ember), modify the `drawTex` callback in `particle_recipes.js` `buildAtoms()`. No regeneration step.

---

## 2. Recipe API

A recipe is a JS function `(scene, ...args) => emitter | emitter[]`. Every recipe:

1. Takes the calling `scene` as its first argument so it can call `scene.add.particles(...)`.
2. Takes geometry args (x, y, or a target sprite for `follow` recipes).
3. Optionally takes an `opts` object with `{ palette, scale, intensity }` overrides.
4. Returns the spawned emitter (or array of emitters when the recipe layers multiple). Finite burst/duration recipes destroy their emitter Game Objects on `complete` and also arm an idempotent safety timeout for `duration + maxLifespan + grace`. Phaser pooling particles does not destroy the emitter itself. `projectileTrail` is continuous and is the sole caller-managed exception.

The 9 named recipes:

```js
recipes.explosion(scene, x, y, opts?)         // small/medium/large via opts.scale
recipes.hitImpact(scene, x, y, opts?)         // hit, hit-spark, base-hit
recipes.projectileTrail(scene, sprite, opts?) // bullet/arrow/elec-proj/fire-proj/ice-proj — follows sprite
recipes.muzzleFlash(scene, x, y, angle, opts?)// muzzle, fires in direction
recipes.pickupGlow(scene, target, opts?)      // pickup/health-glow — follows pickup item briefly then despawns
recipes.slashArc(scene, x, y, angle, length?, opts?)  // sword/claw arcs — length px defaults to 80
recipes.shieldBreak(scene, x, y, opts?)       // ring + chunks
recipes.summonRing(scene, x, y, opts?)        // tower place / spell cast / spawn-in
recipes.dissolve(scene, sprite, opts?)        // ink-style sprite dissolution
recipes.dust(scene, x, y, opts?)              // footstep/landing/slide
```

**Trigger pattern in scene code:**

```js
import { recipes } from '../utils/particle-recipes.js';

// In create() or a collision handler:
recipes.explosion(this, enemy.x, enemy.y, { scale: 1.5 });
recipes.hitImpact(this, bullet.x, bullet.y);
const trail = recipes.projectileTrail(this, projectile, { palette: 'electric' });
projectile.once('destroy', () => trail.destroy());
```

The recipe library is implemented in **`<GAME_DIR>/src/utils/particle-recipes.js`**. In
the canonical source tree, `particle_recipes.js` is the sibling of this guide.
Implement-agent resolves it under its implement `REF_DIR`; scaffold-agent
resolves it from `SCAFFOLD_REF_DIR/../implement/particle_recipes.js` as declared
by the scaffold templates.

---

## 3. Recipe specifications (config blocks)

The specs below explain the intended Phaser 4 ParticleEmitterConfig. The
executable source of truth is sibling `particle_recipes.js`, which also owns the
shared Phaser runtime dependency and wraps every finite emitter with its
`complete` listener and safety timeout. If a spec and the JS disagree, correct
the canonical JS first, then update this guide to describe it; never hand-rewrite
the copied runtime helper from these abbreviated snippets.

### 3.1 explosion (radial burst + flash + smoke + shrapnel)

Three layered emitters: central flash, smoke cloud, rock shrapnel. Optional ring shockwave for `scale >= 1.5`.

```js
// Layer A — central flash (additive, fast fade)
scene.add.particles(x, y, 'atom_radial_flash', {
  lifespan: 120,
  scale: { start: 0.3 * s, end: 1.0 * s, ease: 'cubic.out' },
  alpha: { start: 1.0, end: 0 },
  blendMode: 'ADD',
  emitting: false,
}).explode(1);

// Layer B — smoke cloud (8-12 puffs, slow rise, dark)
scene.add.particles(x, y, 'atom_smoke_puff', {
  lifespan: { min: 800, max: 1200 },
  speed: { min: 80, max: 180 },
  scale: { start: 0.6 * s, end: 1.4 * s },
  alpha: { start: 0.7, end: 0 },
  tint: 0x222222,
  gravityY: -40,
  angle: { min: 0, max: 360 },
  emitting: false,
}).explode(10);

// Layer C — rock shrapnel (8-16 chunks, gravity)
scene.add.particles(x, y, 'atom_rock_chunk', {
  lifespan: { min: 600, max: 900 },
  speed: { min: 200, max: 400 },
  scale: { start: 0.7 * s, end: 0.5 * s },
  rotate: { min: 0, max: 360 },
  alpha: { start: 1, end: 0 },
  gravityY: 600,
  angle: { min: 0, max: 360 },
  emitting: false,
}).explode(12);
```

`opts.scale` (default 1.0) multiplies all sizes. `opts.palette` (default `'fire'`) recolors layer C to `0x222222` for `'smoke'`, `0x88ccff` for `'electric'`, `0xffaaff` for `'magic'`.

For `opts.scale >= 1.5`, also layer D — shockwave ring:

```js
scene.add.particles(x, y, 'atom_ring_shockwave', {
  lifespan: 500,
  scale: { start: 0.2 * s, end: 2.5 * s, ease: 'cubic.out' },
  alpha: { start: 0.8, end: 0 },
  blendMode: 'ADD',
  emitting: false,
}).explode(1);
```

### 3.2 hitImpact (small radial burst — for bullets/melee landing)

Two layers: central flash + 4-6 directional sparks.

```js
// Flash
scene.add.particles(x, y, 'atom_radial_flash', {
  lifespan: 80,
  scale: { start: 0.2, end: 0.6, ease: 'quad.out' },
  alpha: { start: 1.0, end: 0 },
  blendMode: 'ADD',
  emitting: false,
}).explode(1);

// Sparks
scene.add.particles(x, y, 'atom_ember', {
  lifespan: { min: 200, max: 350 },
  speed: { min: 150, max: 280 },
  scale: { start: 0.5, end: 0 },
  alpha: { start: 1, end: 0 },
  blendMode: 'ADD',
  angle: { min: 0, max: 360 },
  emitting: false,
}).explode(6);
```

`opts.palette`: `'fire'` (default ember tint), `'electric'` (atom_electric_spark instead of ember + tint 0x88ccff), `'ice'` (ember tinted 0xaaeeff).

### 3.3 projectileTrail (continuous follow, kill on projectile destroy)

Returns one emitter that **follows the projectile sprite**. Unlike finite recipes, it has no automatic teardown; the caller must destroy it when the projectile is destroyed.

```js
const trail = scene.add.particles(0, 0, 'atom_ember', {
  follow: sprite,
  followOffset: { x: 0, y: 0 },
  lifespan: 250,
  speed: { min: 0, max: 30 },
  scale: { start: 0.4, end: 0 },
  alpha: { start: 0.9, end: 0 },
  blendMode: 'ADD',
  frequency: 18,         // emit every 18ms (~55 trails/sec)
  quantity: 1,
});
return trail;
```

`opts.palette`:
- `'fire'` (default) — `atom_ember` with no tint
- `'electric'` — switches texture to `atom_electric_spark`, tint `0x88ccff`
- `'ice'` — `atom_ember` with tint `0xaaeeff`
- `'ink'` — `atom_smoke_puff` with tint `0x111111`, blendMode `NORMAL` (replaces ADD)
- `'arrow'` — same as `'ink'` but with shorter lifespan 150ms (clean wake)

Caller pattern:
```js
const trail = recipes.projectileTrail(this, bullet, { palette: 'fire' });
bullet.once('destroy', () => trail.destroy());
```

### 3.4 muzzleFlash (cone in direction)

Single explode at gun barrel position, oriented along `angle` (degrees, Phaser convention 0=right).

```js
// Flash
scene.add.particles(x, y, 'atom_radial_flash', {
  lifespan: 80,
  scale: { start: 0.4, end: 0.8 },
  alpha: { start: 1, end: 0 },
  blendMode: 'ADD',
  emitting: false,
}).explode(1);

// Cone of sparks in firing direction
scene.add.particles(x, y, 'atom_ember', {
  lifespan: 150,
  speed: { min: 200, max: 350 },
  scale: { start: 0.3, end: 0 },
  alpha: { start: 1, end: 0 },
  blendMode: 'ADD',
  angle: { min: angle - 25, max: angle + 25 },  // 50° cone
  emitting: false,
}).explode(5);
```

### 3.5 pickupGlow (warm glow following pickup, ~600ms)

Follow target sprite for 600ms then fade.

```js
const glow = scene.add.particles(0, 0, 'atom_glow_orb', {
  follow: target,
  lifespan: 400,
  speed: { min: 0, max: 20 },
  scale: { start: 0.5, end: 0.2 },
  alpha: { start: 0.8, end: 0 },
  blendMode: 'ADD',
  frequency: 50,
  duration: 600,
});
return glow;
```

`opts.palette`: `'health'` → tint `0xffaaaa`, `'mana'` → `0xaaaaff`, `'gold'` → `0xffdd66`, `'green'` → `0x66ff66`.

### 3.6 slashArc (sword/claw streak)

Edge zone along an arc — particles spawn sequentially along a curve in 200ms.

```js
const slash = scene.add.particles(x, y, 'atom_slash_streak', {
  lifespan: 250,
  scale: { start: 1.0, end: 0.3 },
  alpha: { start: 1, end: 0 },
  rotate: angle,                                    // streak orientation
  speed: 0,
  emitZone: {
    type: 'edge',
    source: new Phaser.Geom.Line(
      -length/2 * Math.cos(rad), -length/2 * Math.sin(rad),
       length/2 * Math.cos(rad),  length/2 * Math.sin(rad),
    ),
    quantity: 8,
    seamless: true,
  },
  blendMode: 'ADD',
  emitting: false,
});
slash.explode(8);
return slash;
```

### 3.7 shieldBreak (ring shockwave + chunks)

```js
// Ring
scene.add.particles(x, y, 'atom_ring_shockwave', {
  lifespan: 400,
  scale: { start: 0.3, end: 2.0, ease: 'cubic.out' },
  alpha: { start: 0.9, end: 0 },
  tint: 0x88ccff,
  blendMode: 'ADD',
  emitting: false,
}).explode(1);

// Chunks (smaller than explosion)
scene.add.particles(x, y, 'atom_rock_chunk', {
  lifespan: { min: 500, max: 800 },
  speed: { min: 150, max: 300 },
  scale: { start: 0.4, end: 0.2 },
  rotate: { min: 0, max: 360 },
  alpha: { start: 1, end: 0 },
  tint: 0x88ccff,
  gravityY: 400,
  angle: { min: 0, max: 360 },
  emitting: false,
}).explode(8);
```

### 3.8 summonRing (tower place / spell cast)

Expanding ring + sparkles converging toward center.

```js
// Ring
scene.add.particles(x, y, 'atom_ring_shockwave', {
  lifespan: 600,
  scale: { start: 0.1, end: 1.5, ease: 'cubic.out' },
  alpha: { start: 1, end: 0 },
  tint: 0xffaaff,
  blendMode: 'ADD',
  emitting: false,
}).explode(1);

// Sparkles (edge zone on circle, converge inward)
scene.add.particles(x, y, 'atom_glow_orb', {
  lifespan: 500,
  scale: { start: 0.4, end: 0 },
  alpha: { start: 1, end: 0 },
  blendMode: 'ADD',
  emitZone: {
    type: 'edge',
    source: new Phaser.Geom.Circle(0, 0, 80),
    quantity: 12,
    seamless: true,
  },
  moveToX: 0,            // converge to recipe origin
  moveToY: 0,
  emitting: false,
}).explode(12);
```

### 3.9 dissolve (ink-style sprite dissolution)

Smoke puffs streaming up from sprite's bbox over 800ms while caller fades the sprite alpha.

```js
const dissolve = scene.add.particles(0, 0, 'atom_smoke_puff', {
  follow: sprite,
  lifespan: 600,
  speed: { min: 30, max: 80 },
  angle: { min: -110, max: -70 },     // upward
  scale: { start: 0.4, end: 1.0 },
  alpha: { start: 0.6, end: 0 },
  tint: 0x111111,
  frequency: 30,
  duration: 800,
});
return dissolve;
```

Caller fades the sprite:
```js
const trail = recipes.dissolve(this, enemy);
this.tweens.add({ targets: enemy, alpha: 0, duration: 800, onComplete: () => enemy.destroy() });
```

### 3.10 dust (footstep / landing / slide)

Tiny particles along a horizontal line, brief.

```js
scene.add.particles(x, y, 'atom_dust_mote', {
  lifespan: { min: 250, max: 400 },
  speed: { min: 30, max: 80 },
  scale: { start: 0.6, end: 0 },
  alpha: { start: 0.8, end: 0 },
  tint: 0xaa8866,
  angle: { min: 200, max: 340 },        // upward-ish
  gravityY: 200,
  emitting: false,
}).explode(opts?.intensity || 4);
```

`opts.intensity`: `'walk'` → 2-3 particles, `'run'` → 5-7, `'land'` → 8-12.

---

## 4. Pipeline integration

### 4.1 assets-agent (the "artist")

For the particle strategy, assets-agent produces no FX/atom files. Route bodies
by role; route effects by the selected GDD strategy:

- `category: 'character' | 'enemy' | 'boss' | 'npc'` → i2v via `produce_validated_sprite_action`.
- **Projectile BODY** — visible animated bullets/arrows/fireballs use
  `produce_validated_sprite_action` with `animation_profile:"projectile"`.
  It centers and validates body-only motion; trail/impact/explosion use the
  separate profile/GDD effect strategy.
- Particle-strategy FX/trails/impacts produce no file. A declared flipbook uses
  its file-backed effect route, never the projectile-body animation contract.
- `category: 'prop' | 'tile' | 'tileset' | 'ui'` → existing static-asset route.

If a legacy `assets/particles/` directory exists from a previous pipeline run, leave it alone (or archive it to `assets/.archived_atoms/`) — the new pipeline doesn't read it. If any `atom_*` manifest entries linger, **remove them**: they will cause `gateNoFxSpritesheets` to flag the manifest as stale.

### 4.2 scaffold-agent

When scaffolding a game whose GDD selects particles:

1. Resolve `SCAFFOLD_REF_DIR/../implement/particle_recipes.js` and copy it into
   `<GAME_DIR>/src/utils/particle-recipes.js` verbatim.
2. Keep every Scene stub behavior-free. Do **not** assume a `BootScene`, import
   the helper into a stub, or call `buildAtoms`; the Implement phase owns the
   real entry Scene and runtime initialization.
3. **Do NOT create `assets/particles/`**, do NOT add `atom_*` rows to
   `asset-manifest.json`, and do NOT add per-FX entries (`fx-explosion`,
   `fx-bullet`, `fx-death`, …). Atoms and FX are both purely code.

### 4.3 implement-agent

When wiring effects into a scene:

1. In the actual entry Scene declared by GDD §1, import both symbols at the top
   and initialize atoms once in `create()`:
   ```js
   import { buildAtoms, recipes } from '../utils/particle-recipes.js';
   ```
   In the entry Scene class:
   ```js
   create() {
     buildAtoms(this);
     // remaining authored entry-scene setup
   }
   ```
   `buildAtoms` is idempotent, so a legitimate entry Scene reload safely skips
   textures already in the cache. Other Scenes import `recipes` only; they do
   not duplicate initialization.
2. At every collision / death / pickup / ability trigger, call the matching recipe:
   ```js
   // OLD i2v pattern (don't write this anymore)
   const fx = this.add.sprite(x, y, 'fx-death').play('fx-death-burst');
   fx.on('animationcomplete', () => fx.destroy());

   // NEW particles pattern
   recipes.explosion(this, x, y, { scale: 1.0 });
   ```
3. For continuous trails (projectiles), capture the caller-owned emitter and destroy it with the projectile:
   ```js
   const trail = recipes.projectileTrail(this, bullet, { palette: 'fire' });
   bullet.once('destroy', () => trail.destroy());
   ```

Under the particle strategy, never preload FX sheets or atom files.

### 4.4 design-agent

When particles are selected, each GDD effect row references a recipe name.
Other strategies reference their declared runtime/file source.

```markdown
| Trigger | Recipe | Notes |
|---|---|---|
| Enemy death | `recipes.explosion(scale: 1.0, palette: 'fire')` | dust + chunks for gritty feel |
| Bullet hits enemy | `recipes.hitImpact(palette: 'fire')` | tiny, 6 sparks |
| Tower placed | `recipes.summonRing(tint: 0xffaaff)` | magic-coded color |
```

---

## 5. FX → recipe migration table (for legacy games)

When converting an existing i2v-based game to particles, use this lookup:

| Old fx-* asset | New recipe call |
|---|---|
| `fx-death`, `fx-explosion`, `fx-explosion-sm` | `recipes.explosion(this, x, y)` |
| `fx-big-explosion`, `fx-explosion-lg` | `recipes.explosion(this, x, y, { scale: 1.6 })` |
| `fx-hit`, `fx-hit-spark`, `fx-base-hit` | `recipes.hitImpact(this, x, y)` |
| `fx-bullet`, `fx-arrow`, `fx-enemy-bullet` | `recipes.projectileTrail(this, bullet, { palette: 'fire' })` |
| `fx-elec-proj` | `recipes.projectileTrail(this, bolt, { palette: 'electric' })` |
| `fx-ice-proj` | `recipes.projectileTrail(this, ice, { palette: 'ice' })` |
| `fx-fire-proj` | `recipes.projectileTrail(this, fire, { palette: 'fire' })` |
| `fx-wraith-bolt` | `recipes.projectileTrail(this, bolt, { palette: 'ink' })` |
| `fx-muzzle` | `recipes.muzzleFlash(this, x, y, angle)` |
| `fx-pickup`, `fx-health-glow` | `recipes.pickupGlow(this, item, { palette: 'health' })` |
| `fx-slash` | `recipes.slashArc(this, x, y, angle, length)` |
| `fx-shield-break` | `recipes.shieldBreak(this, x, y)` |
| `fx-place` | `recipes.summonRing(this, x, y)` |
| `fx-grenade` | `recipes.projectileTrail(this, grenade, { palette: 'ink' })` (smoke trail; the explosion happens via `recipes.explosion` on detonation) |
| `fx-ink-dissolve` | `recipes.dissolve(this, sprite)` |

Delete the old `fx-*-sheet.png` and `fx-*-sheet.json` files after the recipe migration is verified — but rename the directory to `.archived_i2v_fx/<name>/` first so a rollback is possible if a recipe doesn't match the GDD's art direction.

---

## 6. Phaser 4 ParticleEmitter — full API reference

This section is a near-verbatim mirror of the official Phaser 4 particles SKILL.md (`https://github.com/phaserjs/phaser/blob/master/skills/particles/SKILL.md`). Every recipe in §3 uses **only** the API documented below — no custom processors, no custom particle classes. Stick to this surface.

### 6.1 Factory

```js
this.add.particles(x, y, texture, config);
// Returns a ParticleEmitter (extends GameObject).
// x, y: world position (defaults 0)
// texture: string key or Texture instance
// config: ParticleEmitterConfig object
```

Mixins available on the returned emitter: `setPosition`, `setScale`, `setDepth`, `setBlendMode`, `setMask`, `setScrollFactor`.

### 6.2 EmitterOp value formats

Most config props (speed, scale, alpha, angle, x, y) accept:

```js
x: 400                                          // static
x: [100, 200, 300]                              // random pick
x: { min: 100, max: 700 }                       // random in range
x: { min: 100, max: 700, int: true }            // random integer
x: { random: [100, 700] }                       // random integer shorthand
scale: { start: 0, end: 1 }                     // ease over lifetime, default linear
scale: { start: 0, end: 1, ease: 'bounce.out' } // custom ease
scale: { start: 4, end: 0.5, random: true }     // random start, ease to end
x: { values: [50, 500, 200], interpolation: 'catmull' }
x: { steps: 32, start: 0, end: 576 }            // sequential stepped
x: { steps: 32, start: 0, end: 576, yoyo: true }
x: { onEmit: (p, k, t, v) => v, onUpdate: (p, k, t, v) => v }  // callbacks
x: (p, k, t, v) => v + 50                       // emit-time shorthand
```

**Emit-only props** (no onUpdate): `angle`, `delay`, `hold`, `lifespan`, `quantity`, `speedX`, `speedY`.
**Emit + Update props** (support start/end + onUpdate): `accelerationX/Y`, `alpha`, `bounce`, `maxVelocityX/Y`, `moveToX/Y`, `rotate`, `scaleX/Y`, `tint`, `x`, `y`.

### 6.3 Flow vs explode

- `frequency >= 0` (default 0) — flow mode, emits `quantity` particles every `frequency` ms.
- `frequency: -1` — explode mode, no auto-flow. Call `explode(count)` to burst.

```js
emitter.flow(100, 5);            // 5 particles every 100ms
emitter.flow(100, 5, 50);        // auto-stop after 50 total
emitter.explode(30, x, y);       // burst at position
emitter.explode(30);             // burst at emitter position
```

### 6.4 Color

`color` array overrides `tint` and interpolates over particle lifetime:

```js
color: [0xfacc22, 0xf89800, 0xf83600, 0x9f0404],
colorEase: 'quad.out',
```

### 6.5 Zones

**Random zone** — needs `getRandomPoint(point)`:
```js
emitZone: { type: 'random', source: new Phaser.Geom.Circle(0, 0, 100) }
```

**Edge zone** — needs `getPoints(quantity, stepRate)`:
```js
emitZone: {
  type: 'edge',
  source: new Phaser.Geom.Circle(0, 0, 150),
  quantity: 48, yoyo: false, seamless: true,
}
```

**Death zone** — needs `contains(x, y)`:
```js
deathZone: { type: 'onEnter', source: new Phaser.Geom.Rectangle(300, 400, 200, 50) }
deathZone: { type: 'onLeave', source: new Phaser.Geom.Circle(400, 300, 150) }
```

### 6.6 Follow

```js
emitter.startFollow(target, offX?, offY?, trackVisible?);
emitter.stopFollow();
// or via config:
{ follow: sprite, followOffset: { x: 0, y: -20 }, trackVisible: false }
```

### 6.7 Lifecycle controls

- `start()`, `stop(killAlive?)`, `pause()`, `resume()`
- `flow(freq, qty?, stopAfter?)`, `explode(count?, x?, y?)`
- `fastForward(ms, delta?)`, `advance: ms` in config (pre-warm)
- `duration: ms` (auto-stop after time), `stopAfter: count` (auto-stop after N emitted)

Stopping emission is not destruction. A finished emitter remains a Game Object
until `.destroy()` runs; the canonical finite recipes perform that teardown on
`complete` with a safety timeout. Continuous emitters remain caller-managed.

### 6.8 Events

```js
emitter.on('start', e => {});
emitter.on('stop', e => {});
emitter.on('complete', e => {});           // last alive particle dies
emitter.on('explode', (e, p) => {});
emitter.on('deathzone', (e, p, zone) => {});
```

### 6.9 Performance

- `reserve(count)` or `reserve: count` in config — pre-allocate particle objects, avoids GC spikes.
- `maxParticles: N` — hard cap on total objects.
- `maxAliveParticles: N` — visible cap.

### 6.10 Gotchas

- `frequency: 0` means **emit every frame** (max rate), not "never". Use `emitting: false` to prevent emission.
- `frequency: -1` is explode mode and does not auto-flow.
- `speed` and `speedX/speedY` are mutually exclusive (`speed` sets `radial: true`, `speedX/speedY` sets `radial: false`).
- `scale` and `scaleX/scaleY` are mutually exclusive (`scale` sets `scaleX` and deactivates `scaleY`).
- `color` array overrides `tint`.
- `moveToX/moveToY` both must be set; overrides angle/speed.
- `emitting: false` ≠ `active: false`. Former stops new particles; latter freezes the entire emitter.
- `'stop'` event fires when emission stops; `'complete'` fires when the last alive particle dies.
- `hold` keeps particle visible and frozen at end of life for the specified ms before death — useful for trail/lingering looks.
- `advance` fast-forwards on creation so particles are visible on frame 1.
- Zone source must implement the right method: `getRandomPoint(point)` for random, `getPoints(qty, stepRate)` for edge, `contains(x, y)` for death.
- `maxParticles` limits total objects allocated. `maxAliveParticles` limits visible alive count.
- Texture key required — pass a valid loaded texture key.

---

## 7. References

- Official Phaser 4 particles SKILL.md: `https://github.com/phaserjs/phaser/blob/master/skills/particles/SKILL.md`
- Canonical recipe library + `buildAtoms`: sibling `particle_recipes.js` for
  implement-agent; `SCAFFOLD_REF_DIR/../implement/particle_recipes.js` for
  scaffold-agent
