# Art Production Plan Guide

Detailed guidance for filling out GDD Section 2 (Visual Style & Art Production Plan). This is the single most impactful section of the GDD — vague asset specs produce vague assets.

Apply `QUALITY_PROFILE_PATH` first. Image/static-lettering/native-size rules in
this guide are `art_directed` defaults. Other profiles may declare live text,
deliberate composition, uniform fit, integer scaling, or flipbook effects;
all still require an explicit final source and no scaffold fallback.

## The Core Principle

**Every visible entity × every visual state = one row in the sprite plan.** Each row must include a runtime key, final runtime path, target frame size, and placeholder spec. If a knight can idle, run, attack, jump, and die, that's 5 rows. If there are 3 enemy types each with idle and death, that's 6 more rows. Miss a row, and that animation won't exist in the game.

## Character Sprite Planning

### Step 1: List all entities

Walk through the game design and list every distinct visible entity:
- Player character(s)
- Each enemy type (basic, fast, ranged, boss...)
- NPCs (shopkeeper, quest giver...)
- Projectiles (bullets, arrows, fireballs...)
- Collectibles (coins, gems, health pickups...)

### Step 2: List all actions per entity

For each entity, list every visual state it can be in:

**Player actions (typical)**:
| Action | When | Required? |
|--------|------|-----------|
| idle | Standing still | Always required |
| run / walk | Moving | Required if player moves |
| jump | Airborne | Required for platform / side beat_em_up |
| attack / shoot | Attacking | Required if combat exists |
| hurt | Taking damage | Recommended |
| death | Dying | Recommended |
| cast | Using ability | If magic/abilities exist |

**Enemy actions (typical)**:
| Action | When | Required? |
|--------|------|-----------|
| idle | Default state | Always required |
| walk / patrol | Moving | Required if enemy moves |
| attack | Dealing damage | If enemy has melee attack |
| death | Defeated | Only when the body itself needs a death animation |
| hurt | Taking damage | Recommended |

**Minimum viable**: Every character and enemy needs at least `idle`. Add a `death` body action only when the design needs visible body motion (collapse, dissolve posture, etc.); runtime death particles in §5 are feedback and do not become sprite assets.

### Step 3: Determine animation characteristics

For each action, note whether it loops and what kind of motion is involved. This helps assets-agent choose appropriate i2v duration and frame rate.

| Action Type | Loop | Typical Motion | Examples |
|-------------|------|----------------|----------|
| Idle | yes (repeat -1) | Subtle, slow | Breathing, sway, blink |
| Movement | yes (repeat -1) | Rhythmic, cyclic | Run, walk, patrol |
| Attack | no (repeat 0) | Quick burst | Slash, shoot, cast |
| Hurt | no (repeat 0) | Brief reaction | Flinch, knockback |
| Death | no (repeat 0) | One-way transition | Collapse, dissolve, explode |
| Projectile | yes (repeat -1) | Continuous motion | Fireball loop, arrow spin |

**Do NOT specify frame counts or grid layouts** — assets-agent determines these from Seedance i2v output (video duration × fps = actual frame count). The GDD needs to describe the action, whether it loops, runtime key, final path, and placeholder.

### Step 4: Choose frame dimensions

Frame dimensions determine the sprite's pixel footprint in the final sprite sheet. All actions for one entity must share the same dimensions.

| Entity Scale | Frame Size | Use For |
|-------------|-----------|---------|
| Small (coins, projectile bodies) | 48×48 or 64×64 | Collectibles, bullets, arrows, fireball bodies |
| Medium (regular enemies, standard props) | 96×96 | Regular enemies, standard props |
| Large (player characters) | 128×128 | Player and similarly-sized protagonists |
| XL (bosses, large creatures) | 192×192 | Bosses, large environmental elements |

**Consistency rule**: All actions for the same entity MUST use the same frame dimensions. A player's idle at 128×128 and run at 192×192 would cause animation flickering.

**Note**: Frame Size is the equal cell size in the final per-action spritesheet, not an instruction to call `setDisplaySize`. Runtime visible size and collision geometry derive from manifest/content-bbox metadata plus authored CONFIG scale; preserve aspect and never use the frame's transparent padding as the body.

### Step 5: Separate body from effects

This is a critical rule. Body action sheets (attack, shoot, cast) must contain **body-only** motion:
- ✅ Knight swinging arm (body movement for attack)
- ❌ Knight with a huge slash arc (the arc forces the body to shrink)

Effects follow `QUALITY_PROFILE` + GDD: art-directed defaults to particle
recipes; other profiles may use particles, masks/shaders, or a declared
flipbook. Physical projectile bodies remain separate sprite identities.

Why? Keeping body art and feedback separate preserves character/projectile scale while avoiding duplicate baked + runtime effects.

## Runtime VFX Planning

### Common VFX checklist

Go through each game event and ask "what does the player SEE when this happens?"

| Game Event | Runtime Feedback | Contract |
|-----------|------------------|----------|
| Player attacks | Slash arc / muzzle flash | declared §5 effect strategy |
| Projectile flies | Body + separate trail | body in §2B; trail in §5 |
| Projectile hits | Impact burst | declared §5 strategy |
| Enemy dies | Explosion / poof / dissolve | §5 effect plus optional body death action |
| Coin collected | Sparkle / pop | declared §5 strategy |
| Player hurt | Red flash / shake | tween/camera feedback |
| Level complete | Celebration | declared §5 strategy |

### VFX conventions

- Every §5 row declares strategy, trigger, parameters, and cleanup.
- Particle/mask/shader strategies have no file asset; a flipbook has one explicit
  file-backed manifest row.
- Projectile bodies remain separate from attached feedback.

## Map & Background Planning

### Genre → Map strategy

| Genre | Layers | What to Generate |
|-----------|--------|-----------------|
| **platform** | 3–4 parallax layers | sky, far-scenery, mid-scenery, (near-detail) |
| **platform (auto-scroll variant)** | 2–3 parallax layers | sky, far-bg, repeating ground |
| **bullet_hell / top beat_em_up / real-time roguelike** | 1 base + separate props | ground-only base, then separate prop sprites |
| **tower_defence** | model-dependent | `authored`: terrain-only base + runtime `levels/map-layout.json`; procedural/board: declared topology/rendering source |
| **puzzle (grid)** | individual tile assets | one generated image per seamless tile type |
| **sandbox / puzzle (physics)** | 1 baked background | Single scene illustration |
| **card / idle** | Minimal | Simple color or gradient, focus on UI assets |

### Layer separation rule

The base/background image must contain ONLY stable non-interactive scenery:
- ✅ Ground, terrain, sky, distant mountains, atmosphere
- ❌ Trees, rocks, crates, platforms, enemies, pickups

Anything the player can interact with, collide with, or that needs to move independently must be a separate prop or game object.

**Gameplay-authoritative geometry.** Anything logic follows/snaps to is data,
not stochastic scenery. `tower_defence + authored` stores it in
`levels/map-layout.json`; other models name their machine-readable topology
authority. Visual geometry must derive from that same source.

### Parallax layer dimensions

All layers for a single level must share the same pixel dimensions (typically matching the game resolution, e.g., 800×600). The scroll factor creates the depth illusion:
- Sky: scrollFactor 0 (fixed)
- Far: scrollFactor 0.2
- Mid: scrollFactor 0.5
- Near: scrollFactor 0.8

## Prop Planning

### Individual asset production

Every prop/tile is generated in its own `produce_static_asset` call. A logical
group may share art direction and naming, but it is never generated as a grid/pack.

| Prop Type | Production | Why |
|-----------|------------|-----|
| Small/compact (rocks, bushes, crates, signs) | One asset per prop type | Independent matte, trim, bbox, retry, and provenance |
| Large/irregular (trees, buildings, bridges) | One asset per object | Full resolution and precise silhouette |
| Variable-width platform | Generate left cap, seamless middle, and right cap separately | Runtime tiles the middle to the authored width |
| Fixed-size platform | One whole-piece asset per platform type | No seams or runtime stitching |
| Collision-bearing object | One asset per object type with `collidable:true` | Precise content bbox |

For a logical left/middle/right platform set, author three §2D rows and three
independent assets. The middle asset MUST be orthographic and horizontally
seamless (see `prompt-rules.md`). Do not ask the image model for a 1×3 strip.

## UI Element Planning

> **Cross-cutting UI rules:**
> 1. Declare every widget's final source (`image`, live text, or
>    nine-slice/composition). `art_directed` requires generated art for functional
>    widgets; other profiles permit their declared alternatives.
> 2. Author sizing from role, never crop `box_px`: native dimension,
>    logical fit box, or base pixels + integer scales by profile.
> 3. When the contract declares frame/fill images, ship and wire both. Other
>    profiles may deliberately compose a continuous meter at runtime.

### Standard HUD elements to consider

| Element | When to Include |
|---------|----------------|
| Health/energy/stamina/boss bar (frame + fill) | Any game with a continuous health/resource meter |
| Progress/loading/integrity/cooldown gauge (frame + fill) | Any game with a continuous percentage/ratio/depletion/progress UI |
| Score display | Any game with scoring |
| Timer | Timed games or survival modes |
| Lives display | Games with multiple lives |
| Minimap | Large-world top-down games |
| Inventory slots | Games with item collection |
| Buttons (Start, Restart, Pause) | Always |

### Screen Lettering / Logos / Art-Words (stylized typography is an ASSET, not a string)

Under `art_directed`, fixed copy is lettering art or baked widget art.
Responsive/minimal may use polished localized live text; pixel art may use
pixel-styled live text or sprite lettering. Bitmap text is unsupported until the
pipeline defines a manifest-backed font-atlas route. Dynamic values are always
runtime-bound and never baked.

**Decision rule — asset vs runtime string (by CONTENT):**

| The on-screen text is… | Treat as | Why |
|---|---|---|
| Game logo / title wordmark (custom/stylized) | Lettering **image asset** (one row, seeded from its Ref Crop) | Default fonts can't reproduce it |
| Big result word ("GAME OVER", "YOU WIN", "LEVEL CLEAR") rendered as art | Lettering **image asset** | Same — it's designed art |
| Styled static prompt/label ("TAP TO START", "HIGH SCORE") with decorative treatment | Lettering **image asset** | Same |
| **Plain static label** (`Integrity:`, `Time Left:`, `SCORE`, section header, instruction, button caption) | Lettering **image asset** OR **baked into the host widget's art** — never `add.text` | Content is fixed at design time → designed typography; `add.text` collapses to Arial AND decouples the text size from the frame |
| **Dynamic value** (score `1,247`, distance `m`, timer, lives count, roster-driven name/cost) | Runtime `add.text` (ALWAYS) — stored + updated via `.setText` | Content changes at runtime — it can never be baked; `.setText` is also how `gateStaticTextAsAsset` distinguishes a live readout from a static label |

**Static/dynamic split (critical).** A heading like "DISTANCE: 1,247m" mixes a styled static label with a live number. Do NOT bake the whole string into one image (the number would freeze). Bake the **static styled portion** as the lettering asset and overlay the **changing value** as a runtime `add.text` positioned over/next to it. The simplest, most robust split is: bake the designed word/logo image (e.g. `ui-gameover-word`), and render every value that changes as live text in the matching style.

**Promotion is mandatory.** Every stylized lettering/logo crop that `ground_crop` produced (e.g. `design/crops/title/game-title-text.png`, `design/crops/gameover/game-over-text.png`) MUST become a row in the 2E table below with a runtime key + that crop as its `Ref Crop`. A grounded crop with no asset row is an **orphan** — it gets generated by nobody and the scene falls back to plain text, losing the design. (The `crop-coverage` gate now blocks on orphaned crops; see assets/design agents.)

**Production + wiring** (assets-agent / implement-agent handle these, but plan for them here):
- Generate from the Ref Crop via `produce_static_asset({ref_image: crop, asset_type: "ui", ...})`; the tool performs edit → matte → trim → optional uniform scale atomically. Mark the GDD/manifest row `decorative: true` (not a tool argument; lettering is never collided with, so it needs no bbox).
- implement-agent loads it as `this.add.image(x, y, key)` foreground UI (with `setScrollFactor(0)` / `setDepth`), and overlays any dynamic value as a separate `add.text`.

**⛔ Sizing — author ONE locked dimension; the crop's aspect supplies the other (non-negotiable).** A lettering/prompt row's §2E `Target size` is a SINGLE locked dimension (`Lock` = width or height) — e.g. `w 360` or `h 102`; you do NOT author a `width×height` pair. assets-agent scales the crop **uniformly** to that locked dimension, so the crop's own **aspect ratio is preserved**. Example: a `454×128` (≈3.55:1) "PRESS ENTER TO CONTINUE" crop with `Target size = w 360` ships ~`360×102`, never a squashed `360×80`.
- **Do NOT author a fixed-height "banner" pair** (e.g. "every prompt is `…×80`") and do NOT reuse the button dimensions below. Authoring an off-aspect w×h forces a **non-uniform resize** at generation time that squashes the lettering flat and destroys legibility — this is the single most common way designed prompts ship broken. One locked dimension makes it structurally impossible.
- If a layout truly needs a fixed box of a different aspect, the asset must be **uniformly scaled + transparently padded** (letterboxed) to fit, never stretched. Prefer simply matching the row dims to the crop's aspect. The `aspect-drift` gate blocks lettering whose final aspect drifted from its source crop.

### Binding Assets — art that overlays a runtime value or holds interactive slots

A *binding asset* is UI art whose **content** (a number/label) or **structure** (how many interactive slots) is owned by the code, not the picture: a **gauge/dashboard** showing a live number, a **button** with a label, a **score/resource readout**, a **tower/unit selection panel** or **item shop** (one card per roster entry), an **ability/control toolbar**. If you bake "VANGUARD TURRET / 150" into the panel — or paint a fixed "75%" onto the gauge, or hardcode 4 card slots — the art and the code drift and nothing reconciles them.

Treat both the value AND the slot count exactly like dynamic data: **do NOT bake them into the image, and do NOT guess where they go in code.** Two sub-contracts, both authored in the GDD **§2E.1 Binding Spec** and both measured from the mockup:

**(A) `anchors` — WHERE each overlaid value sits (fixes "text floats off the gauge / isn't centered on the button").**
- Generate the widget as a **value-less styled frame** — chrome, dividers, ticks, (optionally) a per-option icon, but NO numbers and NO labels.
- The frame alone doesn't say where the number goes. Author one `anchor` per overlaid value: `{ id, binds, xPct, yPct, fontPct, align }`, where `xPct/yPct` are the value's **center as a fraction of the asset box** and `fontPct` is its **height as a fraction of the asset height** — both measured from the mockup (concept-art-guide Step 4c). implement-agent places the runtime `add.text` at that anchor (via `placeUiText`), so it lands on the asset and stays put when the asset is scaled.

**(B) `slots` — HOW MANY interactive regions + WHAT each does (fixes "button count/function ≠ implementation").**
- A repeated panel (cards/shop) declares `slots: { source: "CONFIG.<roster>", action }` — the count is the roster size, laid out in code from `Object.keys(CONFIG.<roster>)`, **never a hardcoded literal**. One slot per entry; each binds a clickable `add.zone` to `action`.
- A fixed control bar (pause/restart/settings) declares an explicit `slots: [{ id, xPct, yPct, wPct, hPct, action }]` — one entry per button, each with the `action` it triggers.
- The roster/affordance set is authored ONCE in §3 CONFIG; the art, the slot count, and the wired functions all derive from it.

`gateUiBinding` (general, all genres) enforces: a value_less asset declares anchors/slots, each anchor's value is rendered as runtime text, a data-driven slot count == its `source` size and is laid out FROM the source, and every slot `action` is wired in `src/`. `gateHudBinding` adds tower-defense-specific checks: the tower panel is value-less, `src/` references `CONFIG.towers`, tower cost is live text, and a legacy numeric slot count (when declared) matches the roster. Data-driven source/roster parity and other live labels remain `gateUiBinding` responsibilities. The VLM element axis then confirms each number visually sits at its anchor and the visible button count/function matches the spec.

This is the semantic twin of the geometry decouple rule: the authoritative fact (the number, the roster size, the action) is owned by code/CONFIG (the SSOT); the generator only makes the non-authoritative frame, and the §2E.1 anchors/slots are the measured contract that joins them.

### UI dimension guidelines (authoritative per-role reference ranges)

Author §2E sizing from role + intended hierarchy, never crop `box_px`. Use
the ranges below as logical display guidance, then encode native target,
logical fit box, or base pixels + integer scales by profile. This is the
single source of truth for per-role sizing ranges; `gdd-template.md` §2.0.1
and `design-agent.md` reference this section.

- Icons / badges (hearts, stars, coins): ~48–64px
- HUD buttons (pause, restart, settings): ~56–64px tall; width from aspect
- Primary menu / CTA buttons: ~48–72px tall; width from label/content and
  aspect (commonly ~160–240px)
- Gauge / meter (energy ring, progress dial): ~80–120px
- Health/energy bars: ~160–280px wide, ~20–32px tall
- Panels (selection/build/context): sized to content; a panel that exceeds
  configured `gates.ui_scale.max_canvas_fraction.panel` is suspicious
- Panel/banner: as a fraction of §1 resolution
- All positions derive from §2.0.2. `CONFIG.ui` carries fit/integer sizing only
  for profiles that authorize runtime scaling.
- All UI elements use `setScrollFactor(0)`, `setDepth(1000+)`

### Functional widget source parity

The twin of "Promotion is mandatory" for lettering, applied to functional widgets. The most-dropped UI work after lettering is shipping bars/buttons/panels as bare primitives that don't match the mockup. To prevent it:

- Ground/promote every `art_directed` widget and every `required:true` crop.
- Wire any declared image as an image; never silently replace it with a primitive.
- For composition/live strategies, declare the source and bindings in §2E.
- Continuous-value frame/fill pairs are mandatory when the registry declares them.

The only UI that legitimately stays pure runtime text is a *plain* readable counter/label with no decorative treatment and no frame (e.g. a corner "Score: 0"). When in doubt, if the mockup gave it a drawn frame/chrome, it's art.

## Asset Budget (First Pass)

`CONFIG_PATH` `design.*` is the sole authority for first-pass entity, action,
sprite-row, background, prop-group, UI, and concept-image limits. Read those
values; do not copy them into the GDD or this guide.

- Runtime FX do not count as sprite-action rows.
- Iteration may expand a verified core, but every genuine first-pass overflow is
  marked `EXTENDED SCOPE` with its count and justification.
- Screen lettering/logo rows are identity art and do not disappear merely
  because the functional-UI limit is reached. Generate crop-seeded lettering
  through `produce_static_asset({ref_image, asset_type:"ui"})`.
- Essential profile-contracted widgets are not dropped or replaced with
  primitives to satisfy a cap. Include them, mark justified functional-HUD
  overflow, and omit secondary chrome first.

---

## Per-Genre Asset Checklists

> **Every checklist's "UI" line implicitly includes screen lettering.** Whenever the concept mockups show a stylized game logo, a result art-word ("GAME OVER" / "VICTORY"), or a styled start prompt, add a 2E lettering row for each (seeded from its crop) — see "Screen Lettering / Logos / Art-Words" above. These are easy to forget because they read like text.
>
> Every UI line uses its declared profile source. Image rows are generated/wired;
> composition/live rows remain explicit and polished.

### Platform
- [ ] Player: idle, run, jump, attack (if combat), hurt, death
- [ ] Each enemy: idle (+ walk/attack; body death only when visually required)
- [ ] Runtime feedback (§5): attack, hit, death, and collect strategies
- [ ] Parallax layers: sky, far, mid, (near)
- [ ] Platform tiles: individual left-cap, middle, and right-cap assets
- [ ] Props: individual environmental assets
- [ ] UI: health bar, score display, start/restart buttons

### Bullet Hell / Roguelike (top-down action)
- [ ] Player: walk (4 directions), idle, attack (if combat)
- [ ] Each enemy: idle, walk (body death only when visually required)
- [ ] Projectile bodies (§2B) plus separate trail/impact/death strategies (§5)
- [ ] Map base: ground-only top-down view
- [ ] Props: individual obstacles and decorations
- [ ] UI: health bar, score, minimap (if applicable)

### Platform — auto-scroll variant
- [ ] Player: run (constant), jump, duck/slide (if applicable)
- [ ] Obstacles: static or simple idle animation
- [ ] Runtime feedback (§5): near-miss, death, and collect strategies
- [ ] Parallax layers: sky, far, mid (repeating)
- [ ] Ground tiles: repeating ground strip
- [ ] UI: score, distance, lives

### Puzzle
- [ ] Tiles: game state tiles (tileset)
- [ ] Pieces/tokens: idle animation for each type
- [ ] Runtime feedback (§5): match/clear and placement strategies
- [ ] Background: simple board or gradient
- [ ] UI: score, moves remaining, level indicator

### Tower Defence
- [ ] Towers: each type idle + fire animation (MANDATORY — placed towers render as these sprites, NEVER primitive `add.rectangle` shapes)
- [ ] Enemies: each type walk (body death only when visually required)
- [ ] Projectile bodies: per tower type, generated with projectile profile
- [ ] Runtime feedback (§5): projectile impact and tower placement strategies
- [ ] Map/topology follows `LEVEL_MODEL`; authored uses terrain-only base +
  `levels/map-layout.json`, other models use their declared source
- [ ] UI: money/wave/selection sources follow the profile; names/costs stay live
  and roster-bound

### Sandbox
- [ ] Interactive objects: idle (each type)
- [ ] Launch/throw animation: player action
- [ ] Runtime feedback (§5): collision and break/shatter strategies
- [ ] Background: baked scene
- [ ] UI: object selector, score/progress

### Card / Idle
- [ ] Cards/items: face sprite for each card/item type
- [ ] Character portraits: if dialogue exists
- [ ] Runtime feedback (§5): card-play and selection-highlight strategies
- [ ] Background: simple themed bg
- [ ] UI: extensive — buttons, panels, text areas, indicators
