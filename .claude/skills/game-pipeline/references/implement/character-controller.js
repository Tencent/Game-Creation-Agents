/**
 * character-controller.js — canonical Phaser 4 Arcade platform (incl. auto-scroll variant) / side beat_em_up movement.
 *
 * This file is the SOURCE OF TRUTH copy. scaffold-agent copies it verbatim into
 * every arcade-gravity game's src/ (the same pattern as particle_recipes.js). If
 * a game's local copy diverges from this one, the local copy is the bug — never
 * hand-edit a game's character-controller.js; change THIS file and re-copy.
 *
 * It implements the industry-standard "forgiving" jump feel so every GCA
 * arcade-gravity game plays the same way instead of each game re-deriving (and getting
 * wrong) coyote time / jump buffering / variable jump:
 *
 *   • Coyote time      — you can still jump for a few ms after walking off a ledge.
 *   • Jump buffer      — a jump pressed just before landing fires on touchdown.
 *   • Variable jump    — releasing jump while rising cuts the ascent (short hop).
 *   • Anti-exploit     — performing a jump ZEROES the coyote timer, so one
 *                        airborne window yields exactly one jump (no chaining a
 *                        second jump off leftover coyote).
 *   • Terminal clamp   — a max fall speed (anti-tunnel + predictable feel).
 *
 * No imports: tuning is passed in (the scene feeds CONFIG.player.controller),
 * so the module is self-contained and safe to copy. See character-controller.md
 * for wiring, the input contract, and moving-platform carry/push/squish.
 */

export class CharacterController {
  // Authored tuning (NOT measured). Mirrors gca.config.yaml `gates.character`;
  // the scene overrides any of these from CONFIG.player.controller (GDD §0.2).
  static DEFAULTS = {
    maxSpeed: 220,          // px/s horizontal run speed
    accel: 2000,            // px/s² ground acceleration toward maxSpeed (also stop friction)
    airAccel: 1200,         // px/s² weaker air control
    jumpVelocity: 520,      // px/s initial upward velocity on jump
    coyoteMs: 100,          // grace window to jump after leaving the ground
    jumpBufferMs: 120,      // pre-land window so an early press still jumps
    variableJump: true,     // release-to-shorten the hop
    jumpCutMultiplier: 0.5, // velocity kept when the rising jump is released
    minJumpMs: 80,          // minimum hold before a variable-jump cut applies
    maxFallSpeed: 1200,     // terminal velocity clamp (px/s)
    oneWayDrop: true,       // down + jump requests a drop-through (scene toggles the collider)
  };

  /**
   * @param {Phaser.GameObjects.Sprite} sprite  an Arcade-physics sprite (has .body)
   * @param {object} tuning  overrides for DEFAULTS (pass CONFIG.player.controller)
   */
  constructor(sprite, tuning = {}) {
    this.sprite = sprite;
    this.tuning = { ...CharacterController.DEFAULTS, ...(tuning || {}) };
    this.coyoteTimer = 0;
    this.jumpBufferTimer = 0;
    this.isJumping = false;
    this.jumpHeldFor = 0;
    this.wantsDrop = false;
    this.state = { grounded: false, jumping: false, falling: false, facing: 1, wantsDrop: false };
  }

  /**
   * Advance one step. Call from the scene's update(time, delta).
   * @param {number} dtMs  the Phaser delta (milliseconds)
   * @param {object} input edges/holds from the scene's keys:
   *   { left, right, down, jumpDown (JustDown edge), jumpHeld (isDown) }
   * @returns {object} state — { grounded, jumping, falling, facing, wantsDrop }
   */
  update(dtMs, input = {}) {
    const body = this.sprite && this.sprite.body;
    if (!body) return this.state;
    const t = this.tuning;
    const dt = Math.min(dtMs, 50) / 1000; // clamp dt spikes (tab refocus) so physics stays stable

    const grounded = body.blocked.down || body.touching.down;
    // Coyote: refill on the ground, otherwise bleed down. Landing also clears the
    // "still jumping" flag so the next press is a fresh jump.
    if (grounded) { this.coyoteTimer = t.coyoteMs; this.isJumping = false; }
    else { this.coyoteTimer = Math.max(0, this.coyoteTimer - dtMs); }
    // Jump buffer: set on the press EDGE (input.jumpDown must be JustDown), else bleed.
    if (input.jumpDown) this.jumpBufferTimer = t.jumpBufferMs;
    else this.jumpBufferTimer = Math.max(0, this.jumpBufferTimer - dtMs);

    // Horizontal: accelerate toward the target run speed (snappy, not instant).
    const dir = (input.right ? 1 : 0) - (input.left ? 1 : 0);
    const target = dir * t.maxSpeed;
    const a = (grounded ? t.accel : t.airAccel) * dt;
    if (target > body.velocity.x) body.velocity.x = Math.min(body.velocity.x + a, target);
    else if (target < body.velocity.x) body.velocity.x = Math.max(body.velocity.x - a, target);
    // Ground friction to a dead stop when no input (no ice-skating).
    if (dir === 0 && grounded) {
      const f = t.accel * dt;
      if (body.velocity.x > 0) body.velocity.x = Math.max(0, body.velocity.x - f);
      else if (body.velocity.x < 0) body.velocity.x = Math.min(0, body.velocity.x + f);
    }

    // Jump: a buffered press + a live coyote window. The jump ZEROES coyote so a
    // single airborne window can never yield a second jump (the anti-exploit rule).
    this.wantsDrop = false;
    if (this.jumpBufferTimer > 0 && this.coyoteTimer > 0) {
      if (t.oneWayDrop && input.down) {
        this.wantsDrop = true;        // scene drops the player through the one-way platform
        this.jumpBufferTimer = 0;
      } else {
        body.velocity.y = -t.jumpVelocity;
        this.isJumping = true;
        this.jumpHeldFor = 0;
        this.jumpBufferTimer = 0;
        this.coyoteTimer = 0;         // ← anti-exploit
      }
    }

    // Variable jump: releasing jump while still rising (after a minimum hold so a
    // tap still clears a small gap) cuts the upward velocity for a short hop.
    if (this.isJumping) {
      this.jumpHeldFor += dtMs;
      if (t.variableJump && !input.jumpHeld && this.jumpHeldFor >= t.minJumpMs && body.velocity.y < 0) {
        body.velocity.y *= t.jumpCutMultiplier;
        this.isJumping = false;       // cut exactly once
      }
      if (body.velocity.y >= 0) this.isJumping = false;
    }

    // Terminal velocity clamp (anti-tunnel + predictable fall feel).
    if (body.velocity.y > t.maxFallSpeed) body.velocity.y = t.maxFallSpeed;

    this.state = {
      grounded,
      jumping: !grounded && body.velocity.y < 0,
      falling: !grounded && body.velocity.y > 0,
      facing: dir || this.state.facing,
      wantsDrop: this.wantsDrop,
    };
    return this.state;
  }
}

/**
 * Moving-platform CARRY. Add a kinematic platform's per-frame motion to a rider
 * standing on it, so the rider travels WITH the platform instead of sliding off.
 * Call in the scene's update for each rider whose body.blocked.down is `platform`.
 * Platforms must be kinematic (immovable + a velocity), e.g. moved with tweens
 * via setVelocity, or use trackPlatform()+position-delta for tween-moved bodies.
 */
export function carryRider(rider, platform, dtMs) {
  const v = platform && platform.body && platform.body.velocity;
  if (v) { const dt = dtMs / 1000; rider.x += v.x * dt; rider.y += v.y * dt; return; }
  // Fallback for tween-moved (velocity-less) platforms: use the tracked delta.
  if (platform && platform._prevX !== undefined) {
    rider.x += platform.x - platform._prevX;
    rider.y += platform.y - platform._prevY;
  }
}

/** Record a tween-moved platform's position so carryRider can use the delta. Call AFTER moving it. */
export function trackPlatform(platform) {
  if (!platform) return;
  platform._prevX = platform.x;
  platform._prevY = platform.y;
}
