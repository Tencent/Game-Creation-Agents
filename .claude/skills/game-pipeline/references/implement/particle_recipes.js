/**
 * particle_recipes.js — canonical Phaser 4 ParticleEmitter recipes for runtime FX.
 *
 * This file is the SOURCE OF TRUTH copy. scaffold-agent copies it verbatim
 * into every new game's src/utils/particle-recipes.js. If a game's local
 * copy diverges from this one, the local copy is the bug.
 *
 * Every recipe takes (scene, ...args, opts?) and returns the spawned emitter
 * (or array of emitters when the recipe layers multiple). Finite burst and
 * duration emitters destroy themselves after completion, with an idempotent
 * safety timeout in case Phaser does not emit `complete`. `projectileTrail`
 * is continuous and remains caller-owned: stop and destroy it with its
 * projectile.
 *
 * Atoms are PROCEDURAL — Implement calls `buildAtoms(scene)` from the actual
 * GDD entry Scene's create() to draw them into the Phaser texture cache.
 * Scaffold only copies this helper. No PNG files, no `generate_image`, no RMBG.
 * Recipes reference the texture KEYs below.
 *
 * In the GCA source tree, recipe specs are maintained in the sibling
 * implement reference `fx-guide.md` §3. A generated game's copied `src/`
 * file has no sibling guide; update the canonical source pair, never only
 * the generated copy.
 *
 * Runtime contract: index.html loads Phaser once through the classic CDN script
 * before the module graph. Generated modules consume `globalThis.Phaser`; they
 * must not use a bare `import 'phaser'` because the static server has no import
 * map or bundling step.
 */

export const ATOM_KEYS = {
  smoke:    'atom_smoke_puff',
  ember:    'atom_ember',
  rock:     'atom_rock_chunk',
  flash:    'atom_radial_flash',
  ring:     'atom_ring_shockwave',
  dust:     'atom_dust_mote',
  electric: 'atom_electric_spark',
  slash:    'atom_slash_streak',
  glow:     'atom_glow_orb',
};

// ──────────────────────────────────────────────────────────────────────
// buildAtoms — draw all 9 atoms procedurally into the texture cache.
// Implement calls this ONCE from the actual GDD entry Scene's create().
// Do not call from preload (there is no async load) or assume a BootScene.
// Idempotent: skips any atom whose texture already exists.
// ──────────────────────────────────────────────────────────────────────
export function buildAtoms(scene) {
  const tex = scene.textures;
  const exists = (k) => tex.exists(k);

  // Canvas helper — creates a CanvasTexture of size n×n (or w×h), runs
  // the draw callback against its 2D context, then commits to the cache.
  // CanvasTexture is the right tool for soft alpha gradients; Graphics
  // would also work but is more verbose and doesn't expose radial fills.
  function drawTex(key, w, h, draw) {
    if (exists(key)) return;
    const t = tex.createCanvas(key, w, h);
    if (!t) return; // very rare — texture key collision; skip silently
    const ctx = t.getContext();
    ctx.clearRect(0, 0, w, h);
    draw(ctx, w, h);
    t.refresh();
  }

  // Radial gradient helper. stops = [{ at, color, alpha }] in [0..1].
  function radial(ctx, cx, cy, r, stops) {
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
    for (const s of stops) {
      g.addColorStop(s.at, `rgba(${s.color[0]},${s.color[1]},${s.color[2]},${s.alpha})`);
    }
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, r * 2, r * 2);
  }

  // 1. atom_radial_flash 128×128 — white hot core fading to transparent
  drawTex(ATOM_KEYS.flash, 128, 128, (ctx) => {
    radial(ctx, 64, 64, 64, [
      { at: 0.00, color: [255, 255, 255], alpha: 1.00 },
      { at: 0.35, color: [255, 244, 200], alpha: 0.85 },
      { at: 0.70, color: [255, 200, 120], alpha: 0.30 },
      { at: 1.00, color: [255, 255, 255], alpha: 0.00 },
    ]);
  });

  // 2. atom_ring_shockwave 128×128 — thin white ring with soft falloff
  drawTex(ATOM_KEYS.ring, 128, 128, (ctx) => {
    // outer falloff
    radial(ctx, 64, 64, 64, [
      { at: 0.00, color: [255, 255, 255], alpha: 0.00 },
      { at: 0.78, color: [255, 255, 255], alpha: 0.00 },
      { at: 0.86, color: [255, 255, 255], alpha: 0.95 },
      { at: 0.94, color: [255, 255, 255], alpha: 0.95 },
      { at: 1.00, color: [255, 255, 255], alpha: 0.00 },
    ]);
  });

  // 3. atom_smoke_puff 128×128 — dark soft blob with low-frequency irregularity
  drawTex(ATOM_KEYS.smoke, 128, 128, (ctx) => {
    // base soft falloff
    radial(ctx, 64, 64, 60, [
      { at: 0.00, color: [40, 40, 40], alpha: 0.90 },
      { at: 0.60, color: [40, 40, 40], alpha: 0.55 },
      { at: 1.00, color: [40, 40, 40], alpha: 0.00 },
    ]);
    // 5 darker lobes for irregular silhouette
    ctx.globalCompositeOperation = 'source-over';
    for (let i = 0; i < 5; i++) {
      const a = (i / 5) * Math.PI * 2;
      const r = 28 + (i % 2) * 8;
      const x = 64 + Math.cos(a) * 18;
      const y = 64 + Math.sin(a) * 18;
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, 'rgba(20,20,20,0.55)');
      g.addColorStop(1, 'rgba(20,20,20,0.00)');
      ctx.fillStyle = g;
      ctx.fillRect(x - r, y - r, r * 2, r * 2);
    }
  });

  // 4. atom_ember 64×64 — hot core → orange → red → transparent
  drawTex(ATOM_KEYS.ember, 64, 64, (ctx) => {
    radial(ctx, 32, 32, 32, [
      { at: 0.00, color: [255, 255, 255], alpha: 1.00 },
      { at: 0.25, color: [255, 220, 120], alpha: 0.95 },
      { at: 0.60, color: [255, 110,  40], alpha: 0.70 },
      { at: 1.00, color: [120,   0,   0], alpha: 0.00 },
    ]);
  });

  // 5. atom_rock_chunk 64×64 — irregular gray polygon, one side lit
  drawTex(ATOM_KEYS.rock, 64, 64, (ctx) => {
    const pts = [
      [20,  8], [42, 12], [56, 28], [50, 50],
      [34, 58], [14, 50], [ 6, 30], [12, 16],
    ];
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.closePath();
    // base fill: linear top-left bright → bottom-right dark
    const g = ctx.createLinearGradient(10, 10, 50, 54);
    g.addColorStop(0.0, '#b8b8b8');
    g.addColorStop(0.5, '#7a7a7a');
    g.addColorStop(1.0, '#3a3a3a');
    ctx.fillStyle = g;
    ctx.fill();
    // dark edge
    ctx.strokeStyle = 'rgba(0,0,0,0.5)';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  });

  // 6. atom_dust_mote 32×32 — tiny brown-grey speck
  drawTex(ATOM_KEYS.dust, 32, 32, (ctx) => {
    radial(ctx, 16, 16, 14, [
      { at: 0.00, color: [138, 106,  64], alpha: 0.95 },
      { at: 0.60, color: [138, 106,  64], alpha: 0.45 },
      { at: 1.00, color: [138, 106,  64], alpha: 0.00 },
    ]);
  });

  // 7. atom_electric_spark 64×64 — jagged zigzag, blue-white core + glow halo
  drawTex(ATOM_KEYS.electric, 64, 64, (ctx) => {
    // soft glow halo first
    radial(ctx, 32, 32, 32, [
      { at: 0.00, color: [180, 220, 255], alpha: 0.45 },
      { at: 1.00, color: [180, 220, 255], alpha: 0.00 },
    ]);
    // zigzag path
    const path = [[6, 12], [22, 26], [16, 34], [34, 44], [28, 52], [56, 56]];
    // outer blue stroke
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(120,200,255,0.85)';
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.moveTo(path[0][0], path[0][1]);
    for (let i = 1; i < path.length; i++) ctx.lineTo(path[i][0], path[i][1]);
    ctx.stroke();
    // inner white core
    ctx.strokeStyle = 'rgba(255,255,255,1.0)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(path[0][0], path[0][1]);
    for (let i = 1; i < path.length; i++) ctx.lineTo(path[i][0], path[i][1]);
    ctx.stroke();
  });

  // 8. atom_slash_streak 96×32 — horizontal elongated white smear
  drawTex(ATOM_KEYS.slash, 96, 32, (ctx) => {
    // ellipse-ish gradient along the streak
    const g = ctx.createRadialGradient(48, 16, 4, 48, 16, 48);
    g.addColorStop(0.00, 'rgba(255,255,255,1.00)');
    g.addColorStop(0.35, 'rgba(255,255,255,0.55)');
    g.addColorStop(1.00, 'rgba(255,255,255,0.00)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 96, 32);
    // squeeze vertically by overlaying transparent top/bottom bands
    const v = ctx.createLinearGradient(0, 0, 0, 32);
    v.addColorStop(0.00, 'rgba(0,0,0,1.0)');
    v.addColorStop(0.35, 'rgba(0,0,0,0.0)');
    v.addColorStop(0.65, 'rgba(0,0,0,0.0)');
    v.addColorStop(1.00, 'rgba(0,0,0,1.0)');
    ctx.globalCompositeOperation = 'destination-out';
    ctx.fillStyle = v;
    ctx.fillRect(0, 0, 96, 32);
    ctx.globalCompositeOperation = 'source-over';
  });

  // 9. atom_glow_orb 96×96 — soft warm glow (lower contrast than flash)
  drawTex(ATOM_KEYS.glow, 96, 96, (ctx) => {
    radial(ctx, 48, 48, 48, [
      { at: 0.00, color: [255, 244, 200], alpha: 1.00 },
      { at: 0.45, color: [255, 215, 120], alpha: 0.55 },
      { at: 1.00, color: [255, 160,  60], alpha: 0.00 },
    ]);
  });
}

/**
 * @deprecated Import `buildAtoms` in the actual GDD entry Scene and call it
 * from create() instead.
 * Kept as a no-op shim so legacy games that still call preloadAtoms in
 * preload() don't break — but they SHOULD switch to buildAtoms.
 */
export function preloadAtoms(scene) {
  // Intentionally empty. Implement owns buildAtoms(entryScene) in create().
  void scene;
}

// Palette → tint lookups. Used by recipes that accept opts.palette.
const PROJECTILE_PALETTES = {
  fire:     { texture: ATOM_KEYS.ember,    tint: 0xffffff, blend: 'ADD' },
  electric: { texture: ATOM_KEYS.electric, tint: 0x88ccff, blend: 'ADD' },
  ice:      { texture: ATOM_KEYS.ember,    tint: 0xaaeeff, blend: 'ADD' },
  ink:      { texture: ATOM_KEYS.smoke,    tint: 0x111111, blend: 'NORMAL' },
  arrow:    { texture: ATOM_KEYS.smoke,    tint: 0x222222, blend: 'NORMAL', lifespan: 150 },
};

const HIT_PALETTES = {
  fire:     { texture: ATOM_KEYS.ember,    tint: 0xffffff },
  electric: { texture: ATOM_KEYS.electric, tint: 0x88ccff },
  ice:      { texture: ATOM_KEYS.ember,    tint: 0xaaeeff },
};

const PICKUP_PALETTES = {
  health: 0xffaaaa,
  mana:   0xaaaaff,
  gold:   0xffdd66,
  green:  0x66ff66,
  default: 0xffeeaa,
};

const EXPLOSION_DEBRIS_TINTS = {
  fire:     0x222222,
  smoke:    0x222222,
  electric: 0x88ccff,
  magic:    0xffaaff,
};

const DUST_INTENSITY_COUNT = {
  walk: 3,
  run:  6,
  land: 10,
};

const EMITTER_DESTROY_GRACE_MS = 250;

function phaserGeometry() {
  const geometry = globalThis.Phaser?.Geom;
  if (!geometry) {
    throw new Error(
      'Phaser.Geom is unavailable. Load the Phaser CDN runtime before particle-recipes.js.',
    );
  }
  return geometry;
}

function maximumDuration(value) {
  if (Number.isFinite(value)) return Math.max(0, value);
  if (!value || typeof value !== 'object') return 0;
  return Object.values(value).reduce(
    (maximum, entry) => Math.max(maximum, maximumDuration(entry)),
    0,
  );
}

function createManagedEmitter(scene, x, y, texture, config) {
  const emitter = scene.add.particles(x, y, texture, config);
  let finalized = false;
  let safetyTimer = null;

  const markDestroyed = () => {
    if (finalized) return;
    finalized = true;
    safetyTimer?.remove?.();
    emitter.off?.('complete', destroyOnce);
    emitter.off?.('destroy', markDestroyed);
  };
  const destroyOnce = () => {
    if (finalized) return;
    finalized = true;
    safetyTimer?.remove?.();
    emitter.off?.('complete', destroyOnce);
    emitter.off?.('destroy', markDestroyed);
    emitter.destroy();
  };

  emitter.once('complete', destroyOnce);
  emitter.once?.('destroy', markDestroyed);
  const timeoutMs = maximumDuration(config.duration)
    + maximumDuration(config.lifespan)
    + EMITTER_DESTROY_GRACE_MS;
  safetyTimer = scene.time?.delayedCall?.(timeoutMs, destroyOnce) || null;
  return emitter;
}

// ──────────────────────────────────────────────────────────────────────
// 1. explosion — radial burst + flash + smoke + shrapnel + (large) ring
//    Replaces fx-death, fx-explosion, fx-explosion-sm, fx-big-explosion,
//    fx-explosion-lg.
// ──────────────────────────────────────────────────────────────────────
export function explosion(scene, x, y, opts = {}) {
  const s = opts.scale || 1.0;
  const palette = opts.palette || 'fire';
  const debrisTint = EXPLOSION_DEBRIS_TINTS[palette] || EXPLOSION_DEBRIS_TINTS.fire;
  const emitters = [];

  // Layer A — central flash (additive, fast fade)
  const flash = createManagedEmitter(scene, x, y, ATOM_KEYS.flash, {
    lifespan: 120,
    scale: { start: 0.3 * s, end: 1.0 * s, ease: 'cubic.out' },
    alpha: { start: 1.0, end: 0 },
    blendMode: 'ADD',
    emitting: false,
  });
  flash.explode(1);
  emitters.push(flash);

  // Layer B — smoke cloud
  const smoke = createManagedEmitter(scene, x, y, ATOM_KEYS.smoke, {
    lifespan: { min: 800, max: 1200 },
    speed: { min: 80, max: 180 },
    scale: { start: 0.6 * s, end: 1.4 * s },
    alpha: { start: 0.7, end: 0 },
    tint: debrisTint,
    gravityY: -40,
    angle: { min: 0, max: 360 },
    emitting: false,
  });
  smoke.explode(10);
  emitters.push(smoke);

  // Layer C — rock shrapnel
  const rock = createManagedEmitter(scene, x, y, ATOM_KEYS.rock, {
    lifespan: { min: 600, max: 900 },
    speed: { min: 200, max: 400 },
    scale: { start: 0.7 * s, end: 0.5 * s },
    rotate: { min: 0, max: 360 },
    alpha: { start: 1, end: 0 },
    tint: debrisTint,
    gravityY: 600,
    angle: { min: 0, max: 360 },
    emitting: false,
  });
  rock.explode(12);
  emitters.push(rock);

  // Layer D — shockwave ring (only for large explosions)
  if (s >= 1.5) {
    const ring = createManagedEmitter(scene, x, y, ATOM_KEYS.ring, {
      lifespan: 500,
      scale: { start: 0.2 * s, end: 2.5 * s, ease: 'cubic.out' },
      alpha: { start: 0.8, end: 0 },
      blendMode: 'ADD',
      emitting: false,
    });
    ring.explode(1);
    emitters.push(ring);
  }

  return emitters;
}

// ──────────────────────────────────────────────────────────────────────
// 2. hitImpact — small radial burst (bullet/melee landing)
//    Replaces fx-hit, fx-hit-spark, fx-base-hit.
// ──────────────────────────────────────────────────────────────────────
export function hitImpact(scene, x, y, opts = {}) {
  const palette = opts.palette || 'fire';
  const cfg = HIT_PALETTES[palette] || HIT_PALETTES.fire;
  const emitters = [];

  // Flash
  const flash = createManagedEmitter(scene, x, y, ATOM_KEYS.flash, {
    lifespan: 80,
    scale: { start: 0.2, end: 0.6, ease: 'quad.out' },
    alpha: { start: 1.0, end: 0 },
    blendMode: 'ADD',
    emitting: false,
  });
  flash.explode(1);
  emitters.push(flash);

  // Sparks
  const sparks = createManagedEmitter(scene, x, y, cfg.texture, {
    lifespan: { min: 200, max: 350 },
    speed: { min: 150, max: 280 },
    scale: { start: 0.5, end: 0 },
    alpha: { start: 1, end: 0 },
    tint: cfg.tint,
    blendMode: 'ADD',
    angle: { min: 0, max: 360 },
    emitting: false,
  });
  sparks.explode(6);
  emitters.push(sparks);

  return emitters;
}

// ──────────────────────────────────────────────────────────────────────
// 3. projectileTrail — continuous follow trail. Caller owns teardown.
//    Replaces fx-bullet, fx-arrow, fx-elec-proj, fx-fire-proj, fx-ice-proj,
//    fx-wraith-bolt, fx-grenade.
// ──────────────────────────────────────────────────────────────────────
export function projectileTrail(scene, sprite, opts = {}) {
  const palette = opts.palette || 'fire';
  const cfg = PROJECTILE_PALETTES[palette] || PROJECTILE_PALETTES.fire;
  const lifespan = cfg.lifespan || 250;

  return scene.add.particles(0, 0, cfg.texture, {
    follow: sprite,
    followOffset: { x: 0, y: 0 },
    lifespan,
    speed: { min: 0, max: 30 },
    scale: { start: 0.4, end: 0 },
    alpha: { start: 0.9, end: 0 },
    tint: cfg.tint,
    blendMode: cfg.blend,
    frequency: 18,   // emit ~55/sec
    quantity: 1,
  });
}

// ──────────────────────────────────────────────────────────────────────
// 4. muzzleFlash — directional cone at gun barrel.
//    Replaces fx-muzzle.
// ──────────────────────────────────────────────────────────────────────
export function muzzleFlash(scene, x, y, angleDeg, opts = {}) {
  const emitters = [];

  // Flash
  const flash = createManagedEmitter(scene, x, y, ATOM_KEYS.flash, {
    lifespan: 80,
    scale: { start: 0.4, end: 0.8 },
    alpha: { start: 1, end: 0 },
    blendMode: 'ADD',
    emitting: false,
  });
  flash.explode(1);
  emitters.push(flash);

  // Cone of sparks
  const sparks = createManagedEmitter(scene, x, y, ATOM_KEYS.ember, {
    lifespan: 150,
    speed: { min: 200, max: 350 },
    scale: { start: 0.3, end: 0 },
    alpha: { start: 1, end: 0 },
    blendMode: 'ADD',
    angle: { min: angleDeg - 25, max: angleDeg + 25 },
    emitting: false,
  });
  sparks.explode(5);
  emitters.push(sparks);

  return emitters;
}

// ──────────────────────────────────────────────────────────────────────
// 5. pickupGlow — warm glow following pickup, ~600ms.
//    Replaces fx-pickup, fx-health-glow.
// ──────────────────────────────────────────────────────────────────────
export function pickupGlow(scene, target, opts = {}) {
  const tint = PICKUP_PALETTES[opts.palette] || PICKUP_PALETTES.default;

  return createManagedEmitter(scene, 0, 0, ATOM_KEYS.glow, {
    follow: target,
    lifespan: 400,
    speed: { min: 0, max: 20 },
    scale: { start: 0.5, end: 0.2 },
    alpha: { start: 0.8, end: 0 },
    tint,
    blendMode: 'ADD',
    frequency: 50,
    duration: 600,
  });
}

// ──────────────────────────────────────────────────────────────────────
// 6. slashArc — sword/claw streak along an edge zone.
//    Replaces fx-slash.
// ──────────────────────────────────────────────────────────────────────
export function slashArc(scene, x, y, angleDeg, length = 80, opts = {}) {
  const rad = (angleDeg * Math.PI) / 180;
  const halfDx = (length / 2) * Math.cos(rad);
  const halfDy = (length / 2) * Math.sin(rad);

  const slash = createManagedEmitter(scene, x, y, ATOM_KEYS.slash, {
    lifespan: 250,
    scale: { start: 1.0, end: 0.3 },
    alpha: { start: 1, end: 0 },
    rotate: angleDeg,
    speed: 0,
    tint: opts.tint || 0xffffff,
    emitZone: {
      type: 'edge',
      source: new (phaserGeometry().Line)(-halfDx, -halfDy, halfDx, halfDy),
      quantity: 8,
      seamless: true,
    },
    blendMode: 'ADD',
    emitting: false,
  });
  slash.explode(8);
  return slash;
}

// ──────────────────────────────────────────────────────────────────────
// 7. shieldBreak — ring shockwave + chunks (cyan-coded).
//    Replaces fx-shield-break.
// ──────────────────────────────────────────────────────────────────────
export function shieldBreak(scene, x, y, opts = {}) {
  const tint = opts.tint || 0x88ccff;
  const emitters = [];

  // Ring
  const ring = createManagedEmitter(scene, x, y, ATOM_KEYS.ring, {
    lifespan: 400,
    scale: { start: 0.3, end: 2.0, ease: 'cubic.out' },
    alpha: { start: 0.9, end: 0 },
    tint,
    blendMode: 'ADD',
    emitting: false,
  });
  ring.explode(1);
  emitters.push(ring);

  // Chunks
  const chunks = createManagedEmitter(scene, x, y, ATOM_KEYS.rock, {
    lifespan: { min: 500, max: 800 },
    speed: { min: 150, max: 300 },
    scale: { start: 0.4, end: 0.2 },
    rotate: { min: 0, max: 360 },
    alpha: { start: 1, end: 0 },
    tint,
    gravityY: 400,
    angle: { min: 0, max: 360 },
    emitting: false,
  });
  chunks.explode(8);
  emitters.push(chunks);

  return emitters;
}

// ──────────────────────────────────────────────────────────────────────
// 8. summonRing — expanding ring + sparkles converging inward.
//    Replaces fx-place.
// ──────────────────────────────────────────────────────────────────────
export function summonRing(scene, x, y, opts = {}) {
  const tint = opts.tint || 0xffaaff;
  const emitters = [];

  // Ring
  const ring = createManagedEmitter(scene, x, y, ATOM_KEYS.ring, {
    lifespan: 600,
    scale: { start: 0.1, end: 1.5, ease: 'cubic.out' },
    alpha: { start: 1, end: 0 },
    tint,
    blendMode: 'ADD',
    emitting: false,
  });
  ring.explode(1);
  emitters.push(ring);

  // Sparkles converging from circle edge to center
  const sparkles = createManagedEmitter(scene, x, y, ATOM_KEYS.glow, {
    lifespan: 500,
    scale: { start: 0.4, end: 0 },
    alpha: { start: 1, end: 0 },
    tint,
    blendMode: 'ADD',
    emitZone: {
      type: 'edge',
      source: new (phaserGeometry().Circle)(0, 0, 80),
      quantity: 12,
      seamless: true,
    },
    moveToX: 0,
    moveToY: 0,
    emitting: false,
  });
  sparkles.explode(12);
  emitters.push(sparkles);

  return emitters;
}

// ──────────────────────────────────────────────────────────────────────
// 9. dissolve — ink-style sprite dissolution. Caller fades sprite alpha.
//    Replaces fx-ink-dissolve.
// ──────────────────────────────────────────────────────────────────────
export function dissolve(scene, sprite, opts = {}) {
  return createManagedEmitter(scene, 0, 0, ATOM_KEYS.smoke, {
    follow: sprite,
    lifespan: 600,
    speed: { min: 30, max: 80 },
    angle: { min: -110, max: -70 },
    scale: { start: 0.4, end: 1.0 },
    alpha: { start: 0.6, end: 0 },
    tint: opts.tint || 0x111111,
    frequency: 30,
    duration: 800,
  });
}

// ──────────────────────────────────────────────────────────────────────
// 10. dust — footstep / landing / slide (tiny mote burst).
// ──────────────────────────────────────────────────────────────────────
export function dust(scene, x, y, opts = {}) {
  const intensity = opts.intensity || 'walk';
  const count = DUST_INTENSITY_COUNT[intensity] || 4;

  const dust = createManagedEmitter(scene, x, y, ATOM_KEYS.dust, {
    lifespan: { min: 250, max: 400 },
    speed: { min: 30, max: 80 },
    scale: { start: 0.6, end: 0 },
    alpha: { start: 0.8, end: 0 },
    tint: opts.tint || 0xaa8866,
    angle: { min: 200, max: 340 },
    gravityY: 200,
    emitting: false,
  });
  dust.explode(count);
  return dust;
}

// ──────────────────────────────────────────────────────────────────────
// Single export bundle for callers that import `recipes`.
// ──────────────────────────────────────────────────────────────────────
export const recipes = {
  explosion,
  hitImpact,
  projectileTrail,
  muzzleFlash,
  pickupGlow,
  slashArc,
  shieldBreak,
  summonRing,
  dissolve,
  dust,
};
