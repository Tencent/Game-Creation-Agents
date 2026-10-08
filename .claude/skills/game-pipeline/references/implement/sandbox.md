# Sandbox Implementation Patterns

> Genre: `sandbox` (Matter.js rigid-body physics is the core mechanic). The player places / builds / breaks / balances / assembles objects that respond to forces, gravity, joints, and constraints (World of Goo, Bridge Constructor, Incredible Machine). Also used by physics `puzzle`. Input is usually mouse drag to aim/place/cut/assemble.

## Matter.js Setup

```javascript
// In main.js config:
physics: {
  default: 'matter',
  matter: { gravity: { y: 1 }, debug: false }
}

// In scene create():
this.matter.world.setBounds(0, 0, CONFIG.screen.width, CONFIG.screen.height);
```

## Decoupled Sprite + Body (Recommended)

**Never use `matter.add.image()` for objects that get destroyed at runtime** (merged fruits, broken blocks, etc.). Its internal `preUpdate` reads `body.position` every frame — if the body is removed mid-step, the game crashes and the entire scene update aborts.

Use a plain Image + standalone Matter body instead:

```javascript
// Spawn
const sprite = this.add.image(x, y, 'key');
sprite.setScale(desiredSize / sprite.width);  // visual only, no physics side-effect

const body = this.matter.add.circle(x, y, radius, {
  restitution: 0.2, friction: 0.3, label: 'myObject'
});
body.ownerRef = { sprite, body, alive: true };  // back-reference

// Sync in update()
update() {
  for (const obj of this.objects) {
    if (!obj.alive) continue;
    obj.sprite.setPosition(obj.body.position.x, obj.body.position.y);
    obj.sprite.setRotation(obj.body.angle);
  }
}

// Destroy safely — no deferred timer needed
this.matter.world.remove(obj.body);
obj.sprite.destroy();
obj.alive = false;
```

### Why not `matter.add.image()`?

| `matter.add.image()` trap | Consequence |
|---------------------------|-------------|
| `setScale()` scales the **physics body** too | Small objects tunnel through walls |
| `preUpdate` reads `body.position` every frame | Crashes if body destroyed mid-step |
| Crash aborts entire `step()` | `Time.Clock` callbacks never fire → permanent freeze |

## Timestamp Cooldowns (Not Timers)

For any gameplay-critical gate (can the player act?), **never** rely on `time.delayedCall`. If an error aborts the scene step, the callback silently never fires and the game freezes.

```javascript
// ✗ BAD — delayedCall can be skipped if an error aborts the frame
this.canDrop = false;
this.time.delayedCall(500, () => { this.canDrop = true; });

// ✓ GOOD — timestamp comparison always works
this.lastDropTime = 0;
dropFruit() {
  if (this.time.now - this.lastDropTime < 500) return;
  this.lastDropTime = this.time.now;
  // ...
}
```

## Visual-Only Animations on Physics Objects

`setScale()` on a MatterImage/MatterSprite changes the physics body. For pop-in or pulse effects, animate `displayWidth`/`displayHeight` instead:

```javascript
// ✗ BAD — shrinks the physics body to near-zero
newFruit.setScale(0.01);
this.tweens.add({ targets: newFruit, scaleX: 1, scaleY: 1, ... });

// ✓ GOOD — physics body untouched
const finalW = sprite.displayWidth, finalH = sprite.displayHeight;
sprite.setDisplaySize(1, 1);
this.tweens.add({ targets: sprite, displayWidth: finalW, displayHeight: finalH, ... });
```

## Drag-and-Launch (Slingshot)

```javascript
create() {
  this.launchPoint = { x: 150, y: 450 };
  this.projectile = this.matter.add.sprite(this.launchPoint.x, this.launchPoint.y, 'ball');
  this.projectile.setStatic(true);
  this.input.on('pointerup', (pointer) => this.launch(pointer));
}

launch(pointer) {
  const dx = this.launchPoint.x - pointer.x;
  const dy = this.launchPoint.y - pointer.y;
  const power = CONFIG.launch.power;
  this.projectile.setStatic(false);
  this.projectile.setVelocity(dx * power, dy * power);
}
```

## Stacking Objects

```javascript
spawnBlock(x, y, w, h) {
  return this.matter.add.rectangle(x, y, w, h, {
    restitution: 0.1, friction: 0.8, density: 0.01
  });
}
```

## Sealed Containers

When building a container from separate wall + floor bodies, they must **overlap at corners**. Two static rectangles sharing a corner vertex leave a diagonal seam — small circles slip through.

Three rules:
1. **Walls extend below the floor** (vertical overlap)
2. **Floor extends wider than the walls** (horizontal overlap)
3. **Physics thickness ≥ 2× smallest body radius**

```javascript
// BAD — corner seam
this.matter.add.rectangle(wallX, midY, 12, height, opts);
this.matter.add.rectangle(midX, floorY, width, 12, opts);

// GOOD — overlap at every joint
const thick = 30;
const extend = 40;
this.matter.add.rectangle(wallX, midY, thick, height + extend, opts);
this.matter.add.rectangle(midX, floorY, width + thick * 2, thick, opts);
```

Always set world bounds as a failsafe:
```javascript
this.matter.world.setBounds(0, 0, CONFIG.screen.width, CONFIG.screen.height, 64);
```
