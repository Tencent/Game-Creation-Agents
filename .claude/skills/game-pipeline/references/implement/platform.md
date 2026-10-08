# Platform Implementation Patterns

> **Allowed dependencies:** `character-controller.md`.
>
> Genre: `platform` (`INTERACTION_PROFILE=arcade-gravity`, `camera.view: side-on`). Covers standard run-and-jump games and the endless-runner variant. Select fixed/follow/pan/auto-scroll camera motion from `camera.behavior`; the auto-scroll variant is distinguished by `camera.behavior: auto-scroll`, not by a combined camera enum. For that variant see the last section.

> The base camera snippet below assumes `camera.behavior: follow`. For `fixed`, omit follow; for `pan`, use the authored pan controls; for `auto-scroll`, use the final section. Changing behavior never changes `camera.view` or `INTERACTION_PROFILE`.

> **If `LEVEL_MODEL=authored`**, use the Level DSL (`src/level/*`) instead of hardcoded layouts. Directory existence never selects routing.

## Physics Setup

```javascript
create() {
  // World bounds (wider than screen for scrolling levels)
  this.physics.world.setBounds(0, 0, CONFIG.levels.width, CONFIG.screen.height);

  // Ground
  const ground = this.add.rectangle(
    CONFIG.screen.width / 2, CONFIG.screen.height - 16,
    CONFIG.levels.width, 32, 0x444444
  );
  this.physics.add.existing(ground, true); // static body

  // Player
  this.player = this.add.rectangle(100, 300, 32, 48, 0x00ff00);
  this.physics.add.existing(this.player);
  this.player.body.setCollideWorldBounds(true);

  // Collisions
  this.physics.add.collider(this.player, ground);

  // Camera follow
  this.cameras.main.startFollow(this.player, true, 0.1, 0.1);
  this.cameras.main.setBounds(0, 0, CONFIG.levels.width, CONFIG.screen.height);

  // Input
  this.cursors = this.input.keyboard.createCursorKeys();
}
```

## Body Sizing for Scaled Sprites (MANDATORY for AI-generated assets)

AI-generated sprite frames are 960×960 or 640×640, displayed at scale ~0.1-0.4. The physics body MUST be set independently from display scale using `contentBbox` from `*-sheet.json` metadata:

```javascript
// After creating sprite with physics
this.player = this.add.sprite(100, 300, 'player');
this.physics.add.existing(this.player);

// Apply display params via the canonical helper — DO NOT inline these calls.
// applyDisplay enforces the load-bearing order:
//   setOrigin → body.setSize(w,h,false) → body.setOffset → setScale
// Reordering (especially setScale before setSize, or setSize without the
// explicit `false` arg) causes "sometimes hits, sometimes misses" collision
// bugs after flip / scene reload. The canonical `runtime/display-utils.js`
// applyDisplay implementation owns this order.
import { applyDisplay } from '../display-utils.js';
applyDisplay(this.player, 'player');
```

NEVER multiply body dimensions by scale — `body.setSize()` operates in the sprite's local frame coordinate space. The visual scale and physics body are INDEPENDENT systems in Arcade Physics. See `../phaser/physics-arcade.md` "Body Size and Sprite Scale Interaction" for details.

---

## Movement + Jump — use the shared `CharacterController`

Do NOT hand-write the jump. scaffold copies `character-controller.js` verbatim into `src/utils/`; construct it once and feed it edges/holds each frame. This gives coyote time, jump buffer, and variable jump for free (a raw `onFloor() && up` jump feels broken — see `character-controller.md` for why).

```javascript
import { CharacterController } from '../utils/character-controller.js';

// create()
this.cc = new CharacterController(this.player, CONFIG.player?.controller);
this.cursors = this.input.keyboard.createCursorKeys();
this.jumpKey = this.input.keyboard.addKey('SPACE');

// update(time, delta)
const input = {
  left: this.cursors.left.isDown, right: this.cursors.right.isDown, down: this.cursors.down.isDown,
  jumpDown: Phaser.Input.Keyboard.JustDown(this.jumpKey), jumpHeld: this.jumpKey.isDown,
};
const s = this.cc.update(delta, input); // sets body velocities; returns { grounded, jumping, falling, facing, wantsDrop }
this.player.setFlipX(s.facing < 0);
```

The controller reads tuning from `CONFIG.player.controller` (seeded from GDD §0.2's `character` block). For one-way drop-through and moving-platform carry/push/squish, see `character-controller.md`.

## Platform Group

```javascript
createPlatforms(layout) {
  this.platforms = this.physics.add.staticGroup();
  layout.forEach(p => {
    const plat = this.add.rectangle(p.x, p.y, p.w, p.h, 0x666666);
    this.platforms.add(plat);
  });
  this.physics.add.collider(this.player, this.platforms);
}
```

## Composite Platform from a 1×3 Strip (left cap / mid / right cap)

When the platform art ships as a **1×3 strip** (`platform-left.png` / `platform-mid.png` /
`platform-right.png`), the three pieces are
generated **independently** and therefore have different native heights, top-surface offsets,
and cap widths. NEVER place them with three ad-hoc `add.image` calls at guessed positions —
that is exactly how the seams end up misaligned (caps oversized, mid not meeting the cap face,
top surface stepping). Compose them **deterministically** instead:

```javascript
// Build ONE platform of arbitrary width from the 1×3 strip. Always go through
// this helper — never place left/mid/right by hand.
//   x, y      = top-left corner of the platform in world space
//   width     = total platform width (>= leftW + rightW)
//   targetH   = uniform display height every piece is scaled to (kills "caps too big")
buildPlatform(x, y, width, targetH) {
  // 1. Scale each cap UNIFORMLY to targetH so all three pieces share one height.
  const leftTex  = this.textures.get('platform-left').getSourceImage();
  const rightTex = this.textures.get('platform-right').getSourceImage();
  const leftW  = leftTex.width  * (targetH / leftTex.height);
  const rightW = rightTex.width * (targetH / rightTex.height);
  const midW   = Math.max(0, width - leftW - rightW); // mid fills the gap between caps

  // 2. setOrigin(0,0) everywhere → caps butt against the mid by their seam edge,
  //    and all three share the same top (y) and bottom (y+targetH): no stepping.
  const left = this.add.image(x, y, 'platform-left')
    .setOrigin(0, 0).setDisplaySize(leftW, targetH);

  // 3. Mid is a TileSprite: it REPEATS the seamless mid tile to fill midW exactly,
  //    instead of stretching one image or stacking several add.image copies.
  const mid = this.add.tileSprite(x + leftW, y, midW, targetH, 'platform-mid')
    .setOrigin(0, 0);

  const right = this.add.image(x + leftW + midW, y, 'platform-right')
    .setOrigin(0, 0).setDisplaySize(rightW, targetH);

  // 4. ONE static body spanning the whole composite (the visual is 3 pieces, the
  //    collision is a single rectangle aligned to the standable top surface).
  const body = this.add.rectangle(x, y, width, targetH, 0x000000, 0).setOrigin(0, 0);
  this.physics.add.existing(body, true);
  body.body.setSize(width, targetH);
  body.body.updateFromGameObject(); // static body must be refreshed after sizing
  this.platforms.add(body);

  return { left, mid, right, body };
}
```

Rules this enforces:
- **Uniform height** — every piece scaled to the same `targetH`, so a cap can never render
  larger than the mid.
- **Seam continuity** — `setOrigin(0,0)` + cumulative x (`x → x+leftW → x+leftW+midW`) makes each
  piece start exactly where the previous ends; no overlap, no gap.
- **Tiled mid** — `TileSprite` repeats the mid tile to any width; the mid art MUST be a
  horizontally seamless tile (`tileset_piece`) whose left and right edges repeat cleanly.
- **Single collision body** — collision is one rectangle on the top surface, decoupled from the
  3-piece visual (matches the "body sized from the standable surface" rule).

## Enemy with Patrol

```javascript
createEnemy(x, y) {
  const enemy = this.add.rectangle(x, y, 28, 28, 0xff0000);
  this.physics.add.existing(enemy);
  enemy.body.setCollideWorldBounds(true);
  enemy.direction = 1;
  enemy.body.setVelocityX(CONFIG.enemies.basic.speed);
  return enemy;
}

updateEnemyPatrol(enemy) {
  // Reverse at world bounds or platform edges
  if (enemy.body.blocked.left || enemy.body.blocked.right) {
    enemy.direction *= -1;
    enemy.body.setVelocityX(CONFIG.enemies.basic.speed * enemy.direction);
  }
}
```

## Collectibles

```javascript
createCoins(positions) {
  this.coins = this.physics.add.staticGroup();
  positions.forEach(p => {
    const coin = this.add.circle(p.x, p.y, 12, 0xffd700);
    this.coins.add(coin);
  });
  this.physics.add.overlap(this.player, this.coins, (player, coin) => {
    coin.destroy();
    this.score += CONFIG.scoring.coinPickup;
    this.scoreText.setText(`Score: ${this.score}`);
  });
}
```

## Health System

```javascript
// In create():
this.playerHealth = CONFIG.player.maxHealth;
this.isInvulnerable = false;

playerHit(damage) {
  if (this.isInvulnerable) return;
  this.playerHealth -= damage;
  this.isInvulnerable = true;
  // Flash effect
  this.tweens.add({
    targets: this.player, alpha: 0.3, duration: 100, yoyo: true, repeat: 5,
    onComplete: () => { this.player.alpha = 1; this.isInvulnerable = false; }
  });
  if (this.playerHealth <= 0) {
    this.scene.start('GameOverScene', { score: this.score, won: false });
  }
}
```

---

## Auto-Scroll Variant (endless runner)

For `platform` games with `camera.view: side-on` and `camera.behavior: auto-scroll`, the world advances automatically and the only input is jump/duck/lane-switch (Dino, Geometry Dash, Doodle Jump). Gravity is lighter (`y: 300–600`); background repeats; obstacles are placed procedurally.

> **If `LEVEL_MODEL=authored`, do not use the patterns below** — consume
> `LevelLoader` / `SpawnScheduler` / `TerrainAssembler`. Other models implement
> their own declared content authority; directory presence is irrelevant.

```javascript
create() {
  this.scrollSpeed = CONFIG.autoScroll.initialSpeed;
  this.score = 0;
  this.player = this.add.rectangle(120, 400, 32, 48, 0x00ff00);
  this.physics.add.existing(this.player);
  this.player.body.setGravityY(CONFIG.physics.gravity);
  this.player.body.setCollideWorldBounds(true);

  // Ground
  this.ground = this.add.tileSprite(400, 580, 800, 40, 'ground_tex');
  this.physics.add.existing(this.ground, true);
  this.physics.add.collider(this.player, this.ground);

  // Obstacles group
  this.obstacles = this.physics.add.group();
  this.physics.add.overlap(this.player, this.obstacles, () => {
    this.scene.start('GameOverScene', { score: this.score, won: false });
  });
  this.spawnTimer = this.time.addEvent({
    delay: CONFIG.autoScroll.spawnInterval,
    callback: this.spawnObstacle, callbackScope: this, loop: true
  });

  // Jump input (space or tap)
  this.input.keyboard.on('keydown-SPACE', () => this.jump());
  this.input.on('pointerdown', () => this.jump());
}

jump() {
  if (this.player.body.onFloor()) {
    this.player.body.setVelocityY(CONFIG.player.jumpForce);
  }
}
```

> For a forgiving jump (coyote time + jump buffer + variable height), prefer the shared `CharacterController` that scaffold copies into `src/utils/` — feed it `{ jumpDown: JustDown(SPACE), jumpHeld: SPACE.isDown }` each frame instead of the raw `onFloor()` check above. An auto-scroll level with no horizontal input just leaves `left/right` false. See `character-controller.md`.

The authored runtime config must define every rate consumed below; do not copy the
loop with an implicit scoring constant:

```javascript
autoScroll: {
  initialSpeed: 220,       // px/s
  acceleration: 8,        // px/s²
  spawnInterval: 1400,    // ms
  pointsPerSecond: 10,    // authored score/s
}
```

```javascript
spawnObstacle() {
  const obs = this.add.rectangle(850, 560, 30, Phaser.Math.Between(30, 60), 0xff4444);
  this.physics.add.existing(obs);
  obs.body.setVelocityX(-this.scrollSpeed);
  obs.body.setAllowGravity(false);
  this.obstacles.add(obs);
}

update(time, delta) {
  const deltaSeconds = delta / 1000;
  // Increase difficulty over time
  this.scrollSpeed += CONFIG.autoScroll.acceleration * deltaSeconds;
  // Keep live obstacles synchronized and clean them from a stable snapshot.
  for (const obstacle of [...this.obstacles.getChildren()]) {
    obstacle.body.setVelocityX(-this.scrollSpeed);
    if (obstacle.x < -50) obstacle.destroy();
  }
  // Scroll ground
  if (this.ground.tilePositionX !== undefined) {
    this.ground.tilePositionX += this.scrollSpeed * deltaSeconds;
  }
  this.score += CONFIG.autoScroll.pointsPerSecond * deltaSeconds;
}
```

`CONFIG.autoScroll.acceleration` is px/s² and
`CONFIG.autoScroll.pointsPerSecond` is the authored time-score rate; neither is
a per-frame increment.
