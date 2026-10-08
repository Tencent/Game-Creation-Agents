# Beat 'Em Up Implementation Patterns

> **Allowed dependencies:** `platform.md`, `character-controller.md`,
> `bullet_hell.md`, `roguelike.md`.
>
> Genre: `beat_em_up` (melee combat, clear rooms / clear waves). Select the implementation variant from the dispatched routing facts recorded in the GDD and scaffolded contracts:
> - **side** (`camera.view: side-on`, `INTERACTION_PROFILE=arcade-gravity`) — a walkable belt; build on `platform.md` movement + `CharacterController` from `src/utils/character-controller.js` for jump feel.
> - **top** (`camera.view: top-down`, `INTERACTION_PROFILE=arcade-no-gravity`) — free 2D movement; build on the 8-direction skeleton in `bullet_hell.md` / `roguelike.md`.
>
> `camera.behavior` is independent: `fixed`, `follow`, `auto-scroll`, or `pan` changes camera motion without changing the side/top movement profile. Validate the dispatched `INTERACTION_PROFILE` against GDD §0.2 and `interaction-contract.json`; never re-derive or replace it from genre defaults.
>
> Either way, combat uses the canonical display helper's **three-box model**: pushbox = physics body, hurtbox = vulnerable rect, hitbox = active-frame attack rect tested during the attack's active window. The source of truth is `runtime/display-utils.js`, copied by Scaffold to `src/display-utils.js`.

> **If `LEVEL_MODEL=authored`**, drive room/encounter/wave layout from the
> Level DSL. Directory existence never selects routing.

## Combat loop (three-box)

Import the canonical helpers from a generated Scene—do not recreate rectangle
math locally:

```javascript
import { applyDisplay, hitboxOverlapsHurtbox } from '../display-utils.js';
```

During Implement, use the scaffold-projected `CONFIG.sprites[entityKey].display`
provisional geometry so combat tests can run before Asset Integration finalizes
per-action metadata. Asset Integration may refine frame-local `hitbox` /
`hurtbox` / `contentBbox` later; do not invent a second overlap implementation.
If a required entity key is missing from `CONFIG.sprites`, return a contract
issue instead of silently falling back to broad display bounds.

```javascript
resolveEntityKey(sprite, fallbackKey) {
  const key = sprite?.entityKey
    || sprite?.getData?.('entityKey')
    || sprite?.name
    || sprite?.getData?.('layoutKey')
    || fallbackKey;
  if (!key || !CONFIG.sprites?.[key]?.display) {
    throw new Error(`Missing display metadata for entity key: ${key || '(empty)'}`);
  }
  return key;
}

// Attack: enable the hitbox only during the authored active window.
attack(attackName = 'light') {
  if (this.attacking) return;
  const attack = CONFIG.attacks[attackName];
  if (!attack) throw new Error(`Unknown attack: ${attackName}`);

  this.attacking = true;
  this.hitTargets = new Set();
  const attackEpoch = this.attackEpoch = (this.attackEpoch ?? 0) + 1;
  const animKey = attack.animationKey || `player_${attackName}`;
  this.player.play(animKey);

  this.time.delayedCall(attack.activeStartMs, () => {
    if (this.attacking && this.attackEpoch === attackEpoch) this.hitActive = true;
  });
  this.time.delayedCall(attack.activeEndMs, () => {
    if (this.attackEpoch === attackEpoch) this.hitActive = false;
  });
  this.player.once(`animationcomplete-${animKey}`, () => {
    if (this.attackEpoch !== attackEpoch) return;
    this.hitActive = false;
    this.attacking = false;
  });
}

updateMeleeHits() {
  if (!this.hitActive) return;

  for (const e of this.enemies.getChildren()) {
    if (!e.active || this.hitTargets?.has(e)) continue;

    const enemyKey = this.resolveEntityKey(e);
    if (hitboxOverlapsHurtbox(this.player, 'player', e, enemyKey, this.hitActive)) {
      this.hitTargets.add(e);
      this.damageEnemy(e);
    }
  }
}

update(time, delta) {
  this.updateMeleeHits();
  // Continue the scene's movement, AI, animation, objective, and UI updates.
  // NEVER return from the whole update() merely because the hitbox is inactive.
}
```

- **pushbox** stays small/stable (Arcade body) so spacing/movement is predictable even as attack frames change.
- **i-frames** disable only the hurtbox after a hit; the pushbox still blocks movement.
- **crowds**: cap simultaneous attackers, stagger enemy approach so the player is not swarmed instantly.

## Committal timing: the attack animation must fit its lock window

An attack is **committal** — the character is locked into it for a fixed time. Two clocks must agree:

- **animation length** = `frameCount / frameRate` (a 24-frame clip at `frameRate: 12` is **2000ms**).
- **logic lock** = how long the state machine commits before returning to idle — either driven BY the animation via `animationcomplete` (the combat loop above), or by a separate authored `CONFIG.attacks.<name>.durationMs`.

The combat loop above ends the attack on `once('animationcomplete-…')`, so the animation *is* the lock and they cannot disagree. **But if you author a separate `durationMs` lock** (per-frame fighting-game data with active `windows`, `toIdle()` when `elapsed >= durationMs`), the animation MUST be time-scaled to that lock — otherwise a fixed `frameRate: 12` on a 24-frame (2s) clip gets cut off at ~frame 4 (still the wind-up) when a 360ms lock fires `toIdle()`, and **the attack looks like it does nothing** (the sprite/clip are fine — the swing is being truncated). This is the #1 "my punch is invisible" bug. Derive the committal clip's rate from its lock:

```js
// One-shot committal action: fit the whole clip into its durationMs lock.
frameRate: frameCount / (CONFIG.attacks[name].durationMs / 1000)   // ← NOT a fixed 12
```

Looping actions (idle / walk / block-hold, `repeat: -1`) have no lock—keep their authored `fps`. If the GDD authors startup/active/recovery phases, `CONFIG.attacks.<name>` owns those phase durations and the animation rate must fit their total lock. Never leave the authored attack clock and animation clock unaligned.

## Encounter gating

Lock the room (walls / camera) until the current wave is cleared, then open the exit and advance. Track `enemiesRemaining`; on 0 → unlock + spawn next wave or transition scene.

## Resources

HP + a combo/rage/special meter that fills on hits and spends on specials. Render HP/meter as HUD bars (value-on-anchor per §2E.1).
