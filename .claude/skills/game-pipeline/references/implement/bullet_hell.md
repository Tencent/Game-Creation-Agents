# Bullet Hell Implementation Patterns

> Genre: `bullet_hell` (arcade-no-gravity, fixed-frame / auto-scroll shooter). The core loop is read deterministic projectile patterns → move a small hurtbox through safe gaps → survive or damage the boss. Pattern schedules, phase thresholds, bullet limits, firing rules, and damage values come from the GDD and `CONFIG`.

> **If `LEVEL_MODEL=authored`**, consume bullet-pattern/wave schedules from the Level DSL. Other models consume their declared schedule authority; directory existence never selects routing.

## Movement + Aim Helpers

Input booleans come from the scaffolded interaction contract. These helpers do not own a lifecycle method; compose them in the single `update(time, delta)` at the end of this file.

```javascript
applyTopDownMovement(actor, input, speed, out) {
  out.set(
    (input.right ? 1 : 0) - (input.left ? 1 : 0),
    (input.down ? 1 : 0) - (input.up ? 1 : 0)
  );
  if (out.lengthSq() > 1) out.normalize();
  actor.body.setVelocity(out.x * speed, out.y * speed);
  return out;
}

aimAtPointer(actor, pointer, out) {
  this.cameras.main.getWorldPoint(pointer.x, pointer.y, out);
  return Phaser.Math.Angle.Between(actor.x, actor.y, out.x, out.y);
}
```

Reuse one `Phaser.Math.Vector2` for movement and one for pointer world coordinates. Never aim with raw screen-space pointer coordinates.

## Player Visual + Small Hurtbox

Use a separate authored hurtbox as the moving Arcade body and keep the visual sprite centered on it. The interaction contract must declare the hurtbox entity/layer; if it does not, return a contract issue instead of inventing collision semantics.

```javascript
createPlayer() {
  const spawn = CONFIG.player.spawn;
  const radius = CONFIG.player.hurtbox.radius;
  this.player = this.add.sprite(spawn.x, spawn.y, 'player');
  this.playerHurtbox = this.add.zone(
    spawn.x, spawn.y, radius * 2, radius * 2
  );
  this.physics.add.existing(this.playerHurtbox);
  this.playerHurtbox.body.setCircle(radius);
  this.playerHurtbox.body.setCollideWorldBounds(true);
  tagEntityInteractionLayer(this.playerHurtbox, 'player_hurtbox');
  this.moveVector = new Phaser.Math.Vector2();
  this.aimPoint = new Phaser.Math.Vector2();
}
```

Enemy projectiles overlap `playerHurtbox`, never the full transparent sprite frame. An optional visible focus/hurtbox marker is UI/art behavior authored by the GDD.

## Bounded Bullet Pool

Dense projectile fields reuse a finite Arcade group. `get()` can return `null` when the authored maximum is active; skipping a spawn is safer than allocating beyond the pool contract. Wire overlaps through the scaffolded interaction contract:

```javascript
import { registerInteractions, tagEntityInteractionLayer } from '../display-utils.js';
createEnemyBulletPool() {
  this.enemyBullets = this.physics.add.group({
    classType: Phaser.Physics.Arcade.Sprite,
    defaultKey: 'enemy-bullet',
    maxSize: CONFIG.bullets.maxActive
  });
  registerInteractions(this, {
    matrix: [{ layers: ['player_hurtbox', 'enemy_bullet'], callback: (_hurtbox, bullet) => {
      bullet.disableBody(true, true);
      this.damagePlayer(CONFIG.bullets.contactDamage);
    } }]
  });
}

spawnEnemyBullet(x, y, angle, speed, lifespanMs) {
  const bullet = this.enemyBullets.get(x, y);
  if (!bullet) return null;
  bullet.enableBody(true, x, y, true, true);
  bullet.setData('expiresAt', this.time.now + lifespanMs);
  tagEntityInteractionLayer(bullet, 'enemy_bullet');
  this.physics.velocityFromRotation(angle, speed, bullet.body.velocity);
  return bullet;
}

recycleEnemyBullets(time) {
  for (const bullet of this.enemyBullets.getChildren()) {
    if (bullet.active && time >= bullet.getData('expiresAt')) {
      bullet.disableBody(true, true);
    }
  }
}
```

Disable a bullet on hit, expiry, or an authored world-bound rule. Do not create/destroy hundreds of GameObjects per pattern.

## Deterministic Patterns

Pattern definitions are data. The same phase, pattern index, and elapsed schedule time must emit the same angles without `Math.random()`.

```javascript
emitRadialPattern(origin, pattern) {
  for (let index = 0; index < pattern.count; index += 1) {
    const angle = pattern.angleOffset
      + (Phaser.Math.PI2 * index) / pattern.count;
    this.spawnEnemyBullet(
      origin.x, origin.y, angle,
      pattern.speed, pattern.lifespanMs
    );
  }
}

emitPattern(patternId, origin) {
  const pattern = CONFIG.bullets.patterns[patternId];
  if (!pattern) throw new Error(`Unknown bullet pattern: ${patternId}`);
  if (pattern.type === 'radial') this.emitRadialPattern(origin, pattern);
  else this.emitAuthoredPattern(pattern, origin);
}
```

Spirals derive their angle from authored schedule time / emission index; aimed spreads derive once from the target angle plus authored offsets. Never accumulate a random frame-time rotation.

Validate positive pattern counts/lifespans, a non-empty phase sequence, and a positive `maxActive` before starting the Scene.

## Boss Phase + Schedule

Each phase owns an ordered pattern sequence and authored delays. Changing health thresholds resets the phase schedule once.

```javascript
startBossPhase(index, time) {
  const phase = CONFIG.boss.phases[index];
  this.bossState.phaseIndex = index;
  this.bossState.patternIndex = 0;
  this.bossState.nextPatternAt = time + phase.startDelayMs;
}

updateBossSchedule(time) {
  const phases = CONFIG.boss.phases;
  let next = phases[this.bossState.phaseIndex + 1];
  while (next && this.boss.health / this.boss.maxHealth <= next.enterAtHealthRatio) {
    this.startBossPhase(this.bossState.phaseIndex + 1, time);
    next = phases[this.bossState.phaseIndex + 1];
  }

  const phase = phases[this.bossState.phaseIndex];
  const catchUpPolicy = CONFIG.boss.catchUp || 'bounded';
  let emitted = 0;
  while (time >= this.bossState.nextPatternAt
    && emitted < (CONFIG.boss.maxPatternsPerFrame || 1)) {
    const entry = phase.sequence[
      this.bossState.patternIndex % phase.sequence.length
    ];
    this.emitPattern(entry.patternId, this.boss);
    this.bossState.patternIndex += 1;
    this.bossState.nextPatternAt += entry.delayAfterMs;
    emitted += 1;
    if (catchUpPolicy === 'skip') break;
  }
}
```

Initialize `bossState` once in `create()`. `catchUp: 'bounded'` emits up to
`CONFIG.boss.maxPatternsPerFrame` overdue patterns per rendered frame;
`catchUp: 'skip'` fast-forwards the schedule to the current time without
emitting a backlog. Never let one overdue frame emit an unbounded pattern burst.

Boss phases are optional. A survival/wave-only game omits this section and drives the same pattern API from its authored wave schedule.

## One Scene Update

Movement, aim, firing, pattern scheduling, and recycling are composed in one lifecycle method. `readMovementActions()` reads semantic actions projected from the interaction contract.

```javascript
update(time, delta) {
  const input = this.readMovementActions();
  this.applyTopDownMovement(
    this.playerHurtbox, input, CONFIG.player.speed, this.moveVector
  );
  const aim = this.aimAtPointer(
    this.playerHurtbox, this.input.activePointer, this.aimPoint
  );
  this.player.setPosition(this.playerHurtbox.x, this.playerHurtbox.y);
  if (CONFIG.player.rotateToAim) this.player.setRotation(aim);

  this.updatePlayerFire(time, aim);
  this.updateBossSchedule(time);
  this.updateAuthoredPatternMotion(time, delta);
  this.recycleEnemyBullets(time);
}
```

If player aiming/firing or a boss is not authored, omit the corresponding calls; do not replace this method with a second `update()`.
