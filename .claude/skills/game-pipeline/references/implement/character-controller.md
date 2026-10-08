# Character Controller — shared platform (incl. auto-scroll variant) / side beat_em_up movement

`character-controller.js` is a **verbatim-copied source-of-truth module** (same pattern as `particle_recipes.js`). scaffold-agent copies it into every arcade-gravity game's `src/utils/`; implement-agent **consumes** it — never hand-writes jump physics, and never edits the game's local copy (change the canonical `references/implement/character-controller.js` and re-copy).

It gives every GCA arcade-gravity game the same "forgiving" feel instead of each game re-deriving (and getting wrong) coyote time, jump buffering, and variable jump.

## Why it exists

The naive jump (`if (onFloor && Space) vy = -jump`) feels bad: miss the ledge by one frame and the jump is eaten; press jump one frame early on landing and nothing happens; every hop is max height. The controller fixes all three with the standard techniques, plus an anti-exploit rule so coyote time can't be abused into a double jump.

## Tuning (authored, not measured)

Pass `CONFIG.player.controller` as the second constructor arg. Defaults mirror `gca.config.yaml` `gates.character`; scaffold seeds `CONFIG.player.controller` from GDD §0.2's `character` block.

| Key | Default | Meaning |
|---|---|---|
| `maxSpeed` | 220 | px/s horizontal run speed |
| `accel` / `airAccel` | 2000 / 1200 | px/s² acceleration toward `maxSpeed` (ground / air); `accel` also brakes to a stop |
| `jumpVelocity` | 520 | px/s initial upward velocity |
| `coyoteMs` | 100 | grace window to jump after leaving a ledge |
| `jumpBufferMs` | 120 | pre-land window so an early press still fires |
| `variableJump` | true | release-to-shorten the hop |
| `jumpCutMultiplier` | 0.5 | upward velocity kept when a rising jump is released |
| `minJumpMs` | 80 | min hold before a variable-jump cut applies (a tap still clears a small gap) |
| `maxFallSpeed` | 1200 | terminal-velocity clamp (anti-tunnel + feel) |
| `oneWayDrop` | true | down + jump requests a drop-through |

## Input contract

`update(dtMs, input)` expects the scene to supply **edges and holds** (it does not read the keyboard itself):

```js
const input = {
  left:    cursors.left.isDown,
  right:   cursors.right.isDown,
  down:    cursors.down.isDown,
  jumpDown: Phaser.Input.Keyboard.JustDown(jumpKey), // EDGE — drives the jump buffer
  jumpHeld: jumpKey.isDown,                           // HOLD — drives variable jump
};
```

`jumpDown` **must** be the `JustDown` edge (not `isDown`), or holding jump re-triggers every frame.

## Wiring (implement-agent)

```js
import { CharacterController } from '../utils/character-controller.js';
import { CONFIG } from '../config.js';

// create()
this.player = this.physics.add.sprite(x, y, 'player');
this.player.body.setCollideWorldBounds(true);
this.cc = new CharacterController(this.player, CONFIG.player?.controller);
this.jumpKey = this.input.keyboard.addKey('SPACE');
this.cursors = this.input.keyboard.createCursorKeys();

// update(time, delta)
const input = {
  left: this.cursors.left.isDown, right: this.cursors.right.isDown, down: this.cursors.down.isDown,
  jumpDown: Phaser.Input.Keyboard.JustDown(this.jumpKey), jumpHeld: this.jumpKey.isDown,
};
const s = this.cc.update(delta, input);
// Drive animation/facing from the returned state (don't recompute it):
this.player.setFlipX(s.facing < 0);
this.player.play(s.grounded ? (input.left || input.right ? 'run' : 'idle') : (s.jumping ? 'jump' : 'fall'), true);
if (s.wantsDrop) this.dropThroughOneWay(this.player); // see below
```

Movement (`actions`) still comes from `interaction-contract.json`; the controller is just the physics behind the `move`/`jump` actions. Colliders are still wired by `registerInteractions` — the controller only sets velocities on the player body.

## One-way platforms (the `collide_oneway` verb)

Wire the player↔oneway collider with the `oneWayProcess` callback from `display-utils.js` (it only blocks when falling onto the top). For **drop-through**, the controller raises `state.wantsDrop` on down+jump; the scene briefly lets the player fall past:

```js
dropThroughOneWay(player) {
  player.body.gcaOneWayDropUntil = this.time.now + 180;
}
```

`oneWayProcess` reads this timestamp and suppresses only the one-way collider
during that window. Do not toggle `body.checkCollision.down`: that body-wide
flag also disables landing on ordinary solid terrain.

## Moving platforms (carry / push / squish)

A moving platform is a **kinematic** body (`immovable = true`, `body.allowGravity = false`, moved by `setVelocity` or a tween). The three interactions:

- **Carry** — a rider standing on the platform must travel WITH it. After physics, for each rider on the platform call `carryRider(rider, platform, delta)` (uses the platform's velocity; for tween-moved platforms call `trackPlatform(platform)` after moving it so the position-delta fallback works). Without carry, a horizontally-moving platform slides out from under the player.
- **Push (push-priority)** — when a platform moves INTO a rider against a wall, the platform wins: move the platform, then resolve the rider out of overlap along the platform's motion axis (or kill the rider if it would be crushed past the wall — see squish). Never let the rider sink into the platform.
- **Squish** — if a rider is overlapping a solid on the side opposite the platform's motion (no room to be pushed), it is being crushed: apply the death/damage outcome from GDD §0.2 (e.g. `player.die()`), don't let bodies interpenetrate.

```js
// update(), AFTER super/physics step:
this.platforms.getChildren().forEach((p) => {
  this.playersOn(p).forEach((rider) => carryRider(rider, p, delta));
  trackPlatform(p); // for tween-moved platforms
});
```

`playersOn(platform)` = riders whose `body.blocked.down` and horizontal overlap match the platform (Arcade exposes `body.blocked`/`touching` after the collide step).

## Gates

`gateInteractionContract` checks the player body has world bounds and that colliders are wired from the matrix; `gateTunnelRisk` uses `maxFallSpeed` reasoning for fast bodies. The controller's feel numbers are authored — no gate measures them, but keeping them in `CONFIG.player.controller` (not magic numbers in the scene) keeps them tunable and auditable.
