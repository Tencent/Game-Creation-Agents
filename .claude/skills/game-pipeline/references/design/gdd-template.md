# GDD Template

Use this structure. Every section is present, but profile/model-inapplicable
subsections are explicitly `N/A` with a reason.

> **This GDD is the single multimodal contract.** Everything downstream — scaffold, implement, assets, verify — reads ONLY this file. So it must **embed** both truths, not merely reference them:
> - **Visual truth** — the selected profile's concept/reference/layout evidence
> plus §2 registries. Mockups and crops are conditional; layout is always explicit.
> - **Mechanics truth** — the mechanics contract (§0.2), sourced from explicit
> Prompt requirements, concept-derived elaboration, and tagged genre defaults.
>
> **Authoring order follows `quality_profile`.** `art_directed` is image-first.
> `responsive`/`pixel_art`/`minimal` may share references. All profiles must
> attempt concept mockup generation for the multimodal GDD. When a mockup exists,
> derive composition and detected positions from it. After bounded
> concept-generation failure, mark only the affected scene
> `waived:concept-generation-unavailable` and continue with complete text/layout
> contracts; never claim that a missing image exists.
>
> **Two VISUAL number classes — sizes AUTHORED, positions MEASURED (READ THIS).** Split the numbers the mockup implies and treat each correctly:
> - **UI sizes are authored, never copied from `box_px`.** `art_directed`
> records one native target dimension; `responsive`/`minimal` record a logical
> fit box; `pixel_art` records base pixels + allowed integer scales. Positions
> come from detection when a reference exists, otherwise from authored normalized
> anchors/regions. Runtime and verification record actual rendered bounds.
> - **Gameplay / content values = AUTHORED, never lifted.** Tuning values (speed, jumpForce, health, damage, spawn rate, score) and **placeholder content numerals** in the art (a HUD "SCORE 9999", "x3", "LV 5") are NOT design facts — never copy them into §3 CONFIG. The image fixes the *look and layout*; the designer authors sizes and what systems *do*.
>
> **Mechanics precedence:** Prompt `[MUST]`/`[MUST_NOT]` hard constraints >
> other explicit Prompt prose > concept-derived additions where the Prompt is
> silent > tagged genre-standard defaults. A concept may create coherent
> gameplay, but may not contradict the Prompt. Record every rule's source.
> (Full authoring guide: `mechanics-contract-guide.md`.)

---

# Game Design Document: [Game Title]

## Genre: [genre_slug]

> Genre is decided by the main agent's Startup routing metadata (text only) and is fixed — one of the 10 slugs (`platform` / `beat_em_up` / `bullet_hell` / `roguelike` / `puzzle` / `tower_defence` / `sandbox` / `simulation` / `card` / `idle`; `custom` is the engineering fallback). Do not silently re-route.

**Design axes** (from Startup routing metadata): control · camera · physics · chance · resource · progression — record the values here so downstream phases share the same design constraints.

**Quality profile**: [art_directed | responsive | pixel_art | minimal]

**Level model**: [authored | procedural | board | none]

## One-Line Fantasy: "[You are a ... who ... to ...]"

## Style Anchor: [One vivid sentence describing the art direction]

---

## Section 0: Game Vision & Core Loop

### Elevator Pitch

[2–3 sentences. What is this game? What makes it fun? What's the hook?]

### Design Pillars (3–5 guiding principles)

1. **[Pillar Name]**: [One sentence explaining the principle]
   — e.g. "Tight Controls: Movement must feel responsive and precise — no input lag, no floaty jumps."
2. ...
3. ...

### Non-Goals (what this game deliberately does NOT do)

- [e.g. "No inventory management", "No cutscenes", "No multiplayer"]

### Player Experience Goals

- **Primary emotion**: [e.g. tension, mastery, flow, wonder, humor]
- **Moment-to-moment feel**: [e.g. "Frantic dodging with brief moments of power"]
- **Session arc**: [e.g. "Start cautious → build confidence → face intense climax → satisfying resolution"]

### Core Gameplay Loop

```
[Action] → [Challenge/Obstacle] → [Feedback] → [Reward] → [Progression] → repeat
```

Example: "Move → Dodge enemies → Collect coins (score flash) → Unlock new area → Harder enemies → repeat"

> When a `gameplay` mockup exists, the loop must match what that image depicts (visible systems — gauges, lanes, formations — imply the mechanics named here).

### Loop Escalation

[How does the loop change over a play session? e.g. "Enemy speed increases 10% per wave, new enemy types introduced at waves 3, 5, 8"]

---

## Section 0.2: Mechanics Contract (the "plays-like" contract — first-class, MANDATORY)

> Mechanics are a first-class truth, equal to the visual truth — they must flow into scaffold (`interaction-contract.json` / `objectives.json`), implement (behaviors/physics), and verify (gameplay + interaction-contract gates), not just sit in prose. Author this from the Prompt + concept following `mechanics-contract-guide.md`. Tag each row MUST / MUST_NOT / SHOULD and give a Source. Concept-derived mechanics are allowed where the Prompt is silent; tagged genre defaults fill only the remaining gaps.

### Physics, Interaction & Collision Model

> scaffold projects this into `.playtest/inputs/interaction-contract.json` (alongside the Controls table — they share one file, since `pick` is both an input and a hit test). The whole sub-section auto-degrades: a pure-pointer game (G53) needs only two layers + one `pick` row; a board/text game with no rigid bodies leaves layers/matrix empty and every physics gate goes N/A. The verb model is **one model, three verbs** — `collide` (rigid separation), `overlap` (detect only), `pick` (pointer hit) — plus `collide_oneway` and `ignore`.

- **Physics**: [arcade-gravity | arcade-no-gravity | matter | none] — must match §1 **and** Startup routing metadata's `interaction_profile`
- **Movement model**: [grid-step | free real-time | rails/slide | path-draw | push | jump+gravity | auto-scroll | pointer-pick | …] — `[Source: Prompt / concept]`

**Interaction layers** — name each kind of thing that can interact (a pointer game can be as small as `star, pointer`):

`terrain, oneway, player, enemy, player_attack, enemy_attack, pickup, hazard`

**Interaction matrix** — for each interacting layer pair, pick exactly one verb `collide | collide_oneway | overlap | pick | ignore` (omit a pair = `ignore`):

| from \ to | terrain | oneway | enemy | pickup | hazard |
|---|---|---|---|---|---|
| player        | collide | collide_oneway | overlap | overlap | overlap |
| enemy         | collide | ignore         | ignore  | ignore  | ignore  |
| player_attack | ignore  | ignore         | overlap | —       | ignore  |

**Per-entity hit shape** (replaces §4's free-text "Physics body"): for every entity name, give `shape: rect|circle`, `body: dynamic|static|kinematic|sensor|none`, `fit: bbox` (size is **MEASURED** from the sprite's `contentBbox` at integrate — never guessed/written here), plus optional `bullet: true` (high-speed projectile) and `min_hit_px` (minimum pointer-target diameter). Example: `player → {shape: rect, body: dynamic, fit: bbox}`, `coin → {shape: circle, body: static, fit: bbox}`, `star → {shape: circle, body: none, fit: bbox, min_hit_px: 40}`.

**Character feel** (platform, incl. auto-scroll variant, and side `beat_em_up`
only): author concrete `coyote_ms`, `jump_buffer_ms`, `variable_jump`,
`max_slope_deg`, and `one_way_drop` values. They are tuning, never measured;
when no design-specific override exists, adopt `CONFIG_PATH`
`gates.character.*` without copying its current defaults into this template.

**World**: `bounds` (which edges collide) + `gravity` (must match §1).

> Two PHYSICS number classes (the rule that lets the contract stay small): **shapes are MEASURED** from the art (`fit: bbox` resolves to the `display-params` body at integrate); **layers / matrix / character feel are AUTHORED** topology/tuning. Never copy pixel sizes into this section.

### Controls & Objectives (the contract scaffold folds into `interaction-contract.json` + `objectives.json`)

**Controls** — every input the player can issue:

| Action | Input (key/mouse/touch) | Effect | MUST / MUST_NOT | Source |
|---|---|---|---|---|
| Move left | ArrowLeft / A | player.vx < 0 | MUST | Prompt |
| Jump | Space (onFloor only) | player.vy = jumpForce | MUST | Prompt |
| … | … | … | … | … |

**Objectives** — what the player must be able to achieve (drives play-agent objectives + gameplay alignment):

| Objective | Reachable by | Success signal | Source |
|---|---|---|---|
| Reach the exit | moving + jumping past hazards | scene transition / win screen | Prompt |
| … | … | … | … |

### Core Rules

- [Rule 1 — e.g. "bonded atoms move as one group" / "enemies spawn in waves of N"] `[MUST]`
- [Rule 2 …]

### Pacing / Difficulty Curve (summary; full curve in §6)

- [e.g. "speed +10% per wave; new enemy type at waves 3/5/8"]

### Win / Lose Conditions

- **Win**: [exact end state + visible signal] `[MUST]`
- **Lose / stall**: [condition, if any]

### Entity Behaviors (summary; per-entity detail in §4)

| Entity | Behavior (observed) | MUST / MUST_NOT | Source |
|---|---|---|---|
| Player | [movement + primary action] | MUST | Prompt |
| [Enemy] | [AI pattern] | MUST | Prompt / concept |
| … | … | … | … |

### Game Feel / Juice intent (full table in §5)

- [e.g. "hit-stop on impact; screen shake on player hurt"] — only if observed or Prompt-required.

### MUST_NOT (forbidden wrong guesses)

- [e.g. "MUST NOT use arrow-key rail movement — the Prompt calls for free path-drawing"] `[Source: Prompt]`

---

## Section 1: Technical Architecture

- **Resolution**: [width]×[height] (e.g., 800×600) — **when concept mockups exist, set this to the chosen mockup's actual dimensions** (the §2.0 canvas every screen was drawn at), not a number picked before the images existed. Genre-default canvas sizes are defined in `concept-art-guide.md` Step 2.

- **Renderer**: Phaser.WEBGL

- **Projection**: **flat 2D (orthographic) — straight side-on or straight top-down. NEVER 2.5D / isometric / axonometric / 3D-perspective / tilted-camera.** The engine draws flat 2D sprites, so the *camera/projection* (and the layout it produces) is flat; any depth the idea implies (tumbling dominoes, "3D ramps", stacked blocks, a lane receding "into" the screen) is realized as a flat 2D view, not a 3D/isometric scene. This constrains the **view**, NOT the art's **finish** — individual sprites can still be richly shaded/rendered (see §2 Dimensionality). Must match §2 and every §2.0 mockup.

- **Physics**: [arcade | matter | none] — must match §0.2

- **Gravity**: { x: [num], y: [num] } or "none"

- **Scene Flow**:

  ```
  BootScene → TitleScene → [GameScene(s)] → GameOverScene
  ```

- **Scene Registry** (exact key strings):
  ["BootScene", "TitleScene", "GameScene", "GameOverScene"]

- **Camera**: [fixed | follow-player | auto-scroll]

- **BootScene purpose**: Preload all assets, show loading bar, then auto-transition to TitleScene

---

## Section 2: Visual Style & Art Production Plan

**Art Direction**: [Detailed visual style description — color palette, mood, art style, references] — **when §2.0 mockups exist, write this from them** (name the dominant colors you actually see), not an abstract style decided up front

**Dimensionality**: **flat 2D VIEW** — mockups AND every generated asset are seen through a flat 2D camera (straight side-on or straight top-down): NO isometric/axonometric, NO 3D perspective / vanishing points, NO tilted horizon / receding ground plane, NO object extruded into a perspective 3D box. This constrains the **view/projection** so the flat Phaser runtime can reproduce the layout — it does **NOT** flatten the *rendering*: individual sprites SHOULD be shaded and rendered for polish (volume, soft/gradient shading, highlights, materials per **Art Style** below), going flat/cel only when the chosen style is explicitly flat/vector. **Depth is painted via shading, never projected via perspective.** (Matches §1 Projection; the runtime is a flat 2D Phaser sprite game.)

**Art Style**: [pixel_art | clean_hd | pixel_inspired | retro_pixel]

**Asset Strategy**: `{ default: generated-image, overrides: { <category/key>:
[generated-image | supplied-image | licensed-image | procedural] } }`

2A–2D rows inherit this map; §2E has its own per-row source strategy. Scaffold
placeholders are never silently promoted to final assets.

### 2.0 Visual Coverage Board (multimodal — drawn FIRST, before the prose)

Record the profile-authorized reference set:
- `art_directed`: one mockup per interactive scene and accountable crops;
- `responsive`/`pixel_art`/`minimal`: shared mockups are allowed through explicit scene mappings;
- any profile: bounded generation failure may use
  `waived:concept-generation-unavailable` for only the affected reference;
  normalized layout and all non-image contracts remain required.
For every crop, set `required:true|false`. Only selected/required crops become
asset obligations outside `art_directed`.

> **Source of truth.** The mockup is authoritative for *layout, composition, palette, on-screen element set, resolution, relative look, and the shape of visible systems*. §3 CONFIG is authoritative for *gameplay/tuning values*. When the image and a value disagree: the image sets the look/layout, and element **positions** are MEASURED off it by `detect_layout` into §2.0.2 (the position SSOT); the designer **authors UI sizes** from each element's role + relative look (do NOT measure them from `box_px` — a loose crop frame, not a size); CONFIG sets the gameplay value. Never lift **placeholder content numerals** (scores/levels/timers painted in the art) from a mockup.

> **Coverage follows the profile.** Never drop a scene to meet a cost cap.
> Shared references must be mapped explicitly. Unavailable references state
> that fact rather than pretending a mockup exists.

Embed each mockup inline below (and again under its §7 scene). The per-scene coverage plan itself is the §2.0.0 table that follows.

```markdown
![title concept](design/title.png)
![gameplay concept](design/gameplay.png)
```

> Per-element crops are referenced in the **Ref Crop** column of tables 2A–2E. Sprite bodies use them through `ref_image` on the validated sprite pipeline; still assets use `produce_static_asset({ref_image: crop, asset_type: <canonical category>})`, which keeps edit → matte → trim → scale staged and fail-closed. Every §2E UI/lettering/widget uses `asset_type:"ui"`. Leave `Ref Crop = —` when no concept covers an element (assets-agent then falls back to text-only generation).

### 2.0.0 Scene Coverage (profile-aware — fill FIRST)

One row per §1 scene. Coverage is `own`, `shared:<reference>`,
`waived:pure-transition`, or `waived:concept-generation-unavailable`. `own` is
mandatory for interactive `art_directed` scenes; shared is valid for
responsive/pixel/minimal. The concept-generation waiver is valid only after
bounded generation failure, and that scene still requires complete §2.0.2 and
§7 contracts.

| Scene (§1 registry key) | Mockup (`design/<slug>.png`) | Coverage | Note |
|---|---|---|---|
| `MainMenuScene` | `design/main-menu.png` | own | — |
| `ModeSelectScene` | `design/mode-select.png` | own | — |
| `MatchScene` | `design/match.png` | own | representative gameplay scene |
| *(game-over overlay)* | `design/gameover.png` | own | overlay state on MatchScene |
| `SplashScene` | — | `waived:pure-transition` | logo only, no interactive UI |
| … (one row per Scene Registry entry) | … | … | … |

### 2.0.1 UI Sizing Rules

Every UI/HUD size is authored from role and intended hierarchy, never copied
from crop bounds. §2E is the single per-element sizing source.

- **Author the size from ROLE + look, never from `box_px`.** The mockup tells you the *relative* size; you pick the px using per-role reference ranges in `asset-plan-guide.md` "UI dimension guidelines". Lock one dimension; the asset's aspect supplies the other.
- `art_directed`: lock one native dimension; asset pipeline scales uniformly.
- `responsive`/`minimal`: author one logical max box and contain uniformly at runtime.
- `pixel_art`: author base pixels plus allowed positive integer scales.
- No profile permits independent X/Y scaling or off-aspect output.
- Font sizes for runtime text are proportional too: a styled label's font height comes from its Target size; a plain counter's `fontSize` is a sane fraction of the canvas (e.g. ~2–3% of canvas height), not an arbitrary `'16px'`.

### 2.0.2 Scene Layout Design (measure positions → the lays-out-like SSOT)

This map is the position source of truth. When a scene has a mockup, seed it
with `detect_layout` and then cleanly author anchors/regions. A scene whose
concept generation failed authors normalized boxes directly and labels their
source `authored`; crop `box_px` is never a position.

```jsonc
mcp__gca__detect_layout({ input: "design/gameplay.png", canvas_width: <§1 W>, canvas_height: <§1 H> })
// → design/gameplay.detect.json:
//   { scene, canvas, elements: [{ key, role, box:[x,y,w,h] (0..1), box_canvas_px, center, origin, anchorTo, kind }] }
```

Detect output's `box_canvas_px` is a canvas-space convenience for human review
and may not share the source mockup's pixel dimensions. No programmatic consumer
may use it; cropping and layout projection consume normalized `box`.

Then AUTHOR one block per scene from the detection — detection *measures*, you *design* (snap a corner widget to its corner with a clean margin, align rows of buttons, resolve detector overlaps, round jitter). One row per element that appears on the scene:

| Element (key) | Role | Source Strategy | box [x, y, w, h] (0..1) | origin | anchorTo | kind |
|---|---|---|---|---|---|---|
| `ui-health-bg` | hud bar | generated-image | [0.016, 0.028, 0.188, 0.033] | [0,0] | top-left | static |
| `ui-score` | text | live-text | [0.84, 0.028, 0.14, 0.04] | [1,0] | top-right | static |
| `ui-title-logo` | logo | generated-image | [0.22, 0.10, 0.56, 0.22] | [0.5,0] | top-center | static |
| `player` | entity | generated-image | [0.10, 0.62, 0.10, 0.18] | [0.5,1] | bottom-left | spawn |
| `enemy-formation` | zone | procedural | [0.10, 0.10, 0.80, 0.30] | [0.5,0.5] | top-center | zone |
| … (one row per §2A–2E element on this scene) | … | … | … | … | … | … |

Fields:

- **`key`** — the §2A/2D/2E asset/object key this element ships as (the SAME key implement names the placed object, so the verify gate can match rendered ↔ contract by name). Every §2A–2E element that appears on the scene gets a row.
- **Identity scope** — `key`/`bind` identities are unique within one scene. A
  file-backed key may appear in multiple scenes when it keeps the same source
  strategy; the same identity cannot change strategy across scenes.
- **`sourceStrategy`** — copy the canonical §2 source strategy for this key:
  `generated-image`, `supplied-image`, `licensed-image`, `procedural`,
  `live-text`, or `composition`. Scaffold projects this exact
  value; it never infers a strategy from the key or `kind`.
- **`box`** — `[x, y, w, h]` as **fractions of the canvas** (0..1, origin top-left), so the contract is resolution-independent. Position is `x,y`; `w,h` lets the gate score containment for `zone` / large elements.
- **`origin`** — the sprite origin Phaser uses to place it (`[0,0]` top-left, `[0.5,0.5]` center, `[1,0]` top-right). `CONFIG.ui` x/y are COMPUTED from `box` + `origin`.
- **`anchorTo`** — the edge/corner the element pins to under responsive scaling (`top-left`, `top-center`, `center`, `bottom-right`, `fill`, …); lets a corner widget stay in its corner on a different canvas.
- **`kind`** — `static` (fixed UI/decor — center point checked within the
  role-scaled layout tolerance), `spawn` (an entity's INITIAL position — the
  same center-point check with `1.25×` tolerance; there is no special
  spawn-frame capture mode), `zone` (a region the game fills — enemy formation,
  lane band; the gate checks containment, not a point), or `cluster` (a **UI
  asset group / Container** — 2+ spatially-adjacent elements that read as one
  component and move/show/scale together; declared as ONE group whose members
  lay out in local coordinates. See the Cluster rows block below).

**Cluster rows = UI asset groups (Containers) — declare the group + its children, not N colliding coordinates.** When 2+ spatially-adjacent elements read as ONE component in the mockup — a same-corner HUD row (day counter + play + pause), a **stat-slot** (icon + label + value), a **meter** (frame + fill + optional number), a **results panel** (backdrop + 3 value rows) — declare a single `kind: cluster` row for the whole group and list its **children** (which lay out in the Container's LOCAL coordinates). Do NOT author a separate absolute `box` per child — hand-authored neighbors are exactly how the "widgets pile up / the value floats off" bug happens (PNGs are wider than the eyeballed boxes; a long value overruns its neighbor). At runtime the group is ONE Phaser Container (via `addUiCluster` / a group helper): moving/showing/scaling/destroying the parent cascades to every child, and children space by their real size + `gap` so intra-group overlap is structurally impossible.

**The group's children include BOTH image children AND pure-text children.** A pure text value (score/timer/lives) or a static art-word title that lives INSIDE the component is a first-class child of the group — not a loose `add.text` at a guessed world coordinate. This is what pulls pure text into the layout contract (today it's a gate blind spot: score/"GAME OVER"/countdown text has no contract identity, so nothing checks it for overlap/overflow).

Row form (simple 1-D group, all-PNG — backward-compatible):

| Element (key) | Role | Source Strategy | box [x, y, w, h] (0..1) | origin | anchorTo | kind | members (order) | gap |
|---|---|---|---|---|---|---|---|---|
| `hud-top-left` | hud cluster | composition | [0.016, 0.028, 0.30, 0.08] | [0,0] | top-left | cluster | ui-day-counter, ui-btn-play, ui-btn-pause | 8 |

Children form (composite group and/or has text children) — the `members` cell says `see children` and a child block follows:

| Element (key) | Role | Source Strategy | box [x, y, w, h] (0..1) | origin | anchorTo | kind | members (order) | gap |
|---|---|---|---|---|---|---|---|---|
| `hud-score-slot` | stat-slot | composition | [0.02, 0.03, 0.16, 0.07] | [0,0] | top-left | cluster | see children | 6 |

```
hud-score-slot (cluster / stat-slot, layout: row)
  children:
    - { kind: image, key: ui-score-icon,  sourceStrategy: generated-image } # a §2E asset key
    - { kind: image, key: ui-score-label, sourceStrategy: generated-image } # static label AS ART
    - { kind: text,  bind: score, sourceStrategy: live-text, maxChars: 6, align: right }
```

- **Child fields.** Every child requires `kind` + `sourceStrategy`.
  `kind:image` references a file-backed §2E `key`; `kind:text` carries `bind`,
  optional `maxChars`/alignment, and its `live-text` source.
  `id` and child-level `role` are not layout-schema fields; the cluster row owns
  the explicit role.
- Image children keep §2E profile sizing. Runtime text children need no PNG but
  remain declared in the cluster/layout contract.
- The cluster `box` is the group's region. `gateLayoutAlignment` checks the
  rendered Container's anchor-point at its declared `origin` (not always its
  center — the union center is emergent from actual child widths + gaps), then
  checks `members[]` presence and declared left-to-right order. It prefers
  captures parented to the cluster but retains a migration fallback to any
  matching instance. For composite `children[]`, `gateUiLayout` separately
  requires every child inside the container with no sibling overlap or overflow.
  `origin`/`anchorTo` work like a normal widget's. Children lay out in local
  coordinates, so the box need not be pixel-perfect around them.
- **When to group:** 2+ elements that (a) sit adjacent AND (b) express one state / must move-show-scale together / share one panel/frame (stat-slot, meter, results panel, toolbar, card, modal). A lone widget stays a normal `static` row. This section only concerns **spatial adjacency** (same container) — do NOT group two elements merely because they are semantically symmetric but spatially separate (e.g. P1 vs P2 health bars on opposite corners are two separate rows/groups, not one).

Rules:

- **MEASURE, then design — never eyeball.** Run `detect_layout`; do not type a position from gut. A slightly-off detection is corrected here (snap / align / round), but the *starting* numbers are measured, not invented.
- **Do not mix the two pixel fields.** Detect output's `box_canvas_px` is
  `[x,y,w,h]` on the authored canvas and is human-review only. `crops.json`
  `box_px` is `[x0,y0,x1,y1]` on the source mockup after crop padding; the
  element-visual-diff runner consumes it to clip the matching runtime region.
  Neither field is a runtime position or a §2E target size. Runtime position
  comes ONLY from normalized `box` in `detect_layout` / this map.
- **Every §2A–2E element corresponds to a row** (UI + entity + prop +
  decorative) — `gateLayoutAlignment` enforces coverage and position
  (kind-aware: center point within tolerance for `static`, the same check with
  `1.25×` tolerance for `spawn`, centroid containment for `zone`, and declared
  origin anchor + `members[]` presence/order for `cluster`). `gateUiLayout`
  separately enforces composite `children[]` containment, overlap, and overflow.
  A missing row is an un-verified placement. (A cluster member/child is covered
  by its cluster row's `members`/`children` list instead of its own row —
  including text children, which have no §2E row.)
- **One asset spine: crop → §2 row → §2.0.2 row → §7 placement.** These four are the SAME set of keys, not four independent lists. Every crop maps to a §2 row (or is waived — `crop-coverage`); every §2A–2E row has a §2.0.2 position row (this rule); §7 only *places* keys that already exist here (§7 keys ⊆ §2.0.2 keys ⊆ §2 keys). So an asset you decide to show belongs in §2 (what+look+size) AND §2.0.2 (where) — never introduced for the first time in a §7 `add.image`. If §7 authoring surfaces a needed asset, backfill §2 + §2.0.2 first.
- **Downstream derivation.** `CONFIG.ui` x/y and §7 positions are COMPUTED from
  this map (a top-left `origin:[0,0]` widget → `x = box.x·W, y = box.y·H`; a
  centered widget → `x,y = center·[W,H]`; a `cluster` row →
  `CONFIG.ui['<clusterKey>'] = { x, y, origin, layout, gap, children: [...] }`
  where each child carries its `key`|`bind`+`maxChars`). scaffold projects this
  section into `.playtest/inputs/layout-contract.json` (a cluster projects as one
  group entry with its children, so **text children get a contract identity** —
  not a hardcoded coordinate); implement builds each cluster as ONE Phaser
  Container and lays children out in local coordinates (via `addUiCluster` /
  group helper), so moving/showing/scaling the parent cascades and a long text
  value re-flows instead of overrunning. `gateLayoutAlignment` uses
  `gates.layout.tolerance` for center/region checks (`spawn` multiplies it by
  `1.25`; clusters use the declared-origin anchor plus `members[]`
  presence/order). `gateUiLayout` owns composite child containment,
  sibling-overlap, and overflow checks. `gateLayoutAlignment`'s rendered-bounds
  pass also enforces §2.0.1 sizing: only `art_directed` requires display size =
  texture px; responsive/minimal use uniform fit and pixel art uses an allowed
  integer scale.
- Dynamic values stay live. Fixed copy follows `QUALITY_PROFILE`: art-directed
  uses image art; other profiles may use declared live text or sprite lettering.

All profiles (including `minimal`) must attempt concept mockup generation for the
multimodal GDD. At least one successful mockup is required unless every
interactive reference attempt exhausts bounded generation and uses the exact
failure waiver. `minimal` may share references across scenes (like
responsive/pixel_art), and only `required:true` crops become asset obligations.

### Scope of This Section

This section defines **what** assets the game needs and **how they should look** (art direction). It does NOT define:
- File paths or directory structure beyond runtime keys/paths (assets-agent owns production paths)
- Generation prompts or parameters (assets-agent decides)
- Frame counts or sheet layouts (determined by i2v video output)
- Post-processing pipeline choices (assets-agent decides)

For detailed asset planning guidance (entity/action coverage, body vs effect
separation, binding assets, per-genre checklists), see `asset-plan-guide.md`.

assets-agent reads this section as its asset registry, art direction input, and Ref-Crop seed map.
scaffold-agent converts this section into `assets/asset-manifest.json`, the shared contract used by implement-agent placeholders and assets-agent output paths.

### Type Conventions (for placeholder strategy only)

For scaffold-only fallback tokens, use:

- `shape(w,h,color)` — Phaser rectangle placeholder. e.g. `shape(32,48,0x00ff00)`
- `circle(r,color)` — Phaser circle placeholder. e.g. `circle(12,0xffd700)`
- `color(hex)` — Solid background fill. e.g. `color(0x1a1a2e)`

Even when `Asset Strategy` is `generated`, include scaffold-only fallback tokens.

### 2A: Character & Entity Sprite Plan

List every visible entity/action. Assets-agent generates only rows whose
inherited source strategy is `generated-image`.

| Entity | Action | View | Frame Size | Key | Ref Crop | Runtime Path | Placeholder | Description |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Player | idle | side | 128×128 | player | `design/crops/gameplay/player.png` | assets/sprites/player/idle-sheet.png | shape(128,128,0x4488ff) | Breathing motion, subtle sway |
| Player | run | side | 128×128 | player | `design/crops/gameplay/player.png` | assets/sprites/player/run-sheet.png | shape(128,128,0x4488ff) | Full run cycle |
| Player | attack | side | 128×128 | player | `design/crops/gameplay/player.png` | assets/sprites/player/attack-sheet.png | shape(128,128,0x4488ff) | Body only — NO weapon arc |
| Enemy (Slime) | idle | side | 96×96 | enemy_slime | `design/crops/gameplay/slime.png` | assets/sprites/enemy_slime/idle-sheet.png | shape(96,96,0x55aa55) | Squish bounce |
| Boss | idle | side | 192×192 | boss | `design/crops/gameplay/boss.png` | assets/sprites/boss/idle-sheet.png | shape(192,192,0xaa4455) | Large creature breathing loop |
| ... | ... | ... | ... | ... | ... | ... | ... | ... |

**Column definitions**:

- **Entity**: The game entity name
- **Action**: The animation action name; generated-image rows use one validated action pipeline per action
- **View**: `side` | `topdown` | `3/4` — camera angle for generation
- **Frame Size**: Target sprite frame dimensions in the final sprite sheet
- **Key**: The entity key — all actions for the same entity share one key
- **Ref Crop**: The grounded concept crop for this entity (from `mcp__gca__ground_crop`, usually off the `gameplay` mockup). assets-agent seeds the sprite from it (`ref_image=crop`) so it matches the concept. All actions of one entity share its Ref Crop. Leave `—` only when no concept covers it (assets-agent falls back to text-only).
- **Runtime Path**: The final per-action `*-sheet.png`; the same-basename JSON sidecar is mandatory.
- **Placeholder**: Scaffold/implement fallback used only until asset integration.
- **Description**: Visual description of the motion (helps assets-agent write i2v prompts)

**Rules**:

- Every playable character MUST have at minimum: idle + at least one movement action
- Every enemy MUST have at minimum idle; add death only when body animation is needed
- Body action sheets contain body-only motion; separate effects follow §5's profile strategy
- All actions for the same entity MUST use the same Frame Size (consistency requirement)
- Do NOT specify frame count or grid layout — assets-agent derives them from i2v output. DO specify the deterministic final runtime path and placeholder contract.
- **SCOPE LIMIT (first pass)**: Section 2A + 2B combined must not exceed `design.max_sprite_action_rows`. Entity/action limits come from `gca.config.yaml`; projectile bodies count as generated sprite rows, runtime VFX do not.

### 2B: Projectile Body Sprite Plan (optional — NO runtime VFX rows)

| Projectile Body | Owner/Trigger | Action | Frame Size | Key | Ref Crop | Runtime Path | Placeholder | Description |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Fireball body | Player cast | fly | 48×48 | projectile-fireball | `design/crops/gameplay/fireball.png` | assets/sprites/projectile-fireball/fly-sheet.png | shape(48,48,0xff6633) | Centered free-flying body only; no trail or impact |
| Arrow body | Player attack | fly | 64×32 | projectile-arrow | `—` | assets/sprites/projectile-arrow/fly-sheet.png | shape(64,32,0xccaa66) | Arrow body/fletching only; no muzzle flash or hit burst |
| ... | ... | ... | ... | ... | ... | ... | ... | ... |

(Use `—` when the game has no generated projectile body.)

**Rules**:

- Only physical projectile bodies rendered as sprites belong here. Generate them with `animation_profile: "projectile"`.
- Do not list effects as projectile-body rows. File-backed flipbooks, when the
  profile declares them, live in the separate §5 effect strategy.
- Projectile body frames contain the body only; attached VFX are emitted at runtime.

### 2C: Map & Background Plan

| Layer/Asset | Type | Dimensions | Key | Ref Crop | Runtime Path | Placeholder | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Sky | parallax | 800×600 | map-sky | `—` | assets/maps/map-sky.png | color(0x87ceeb) | Gradient sky, no objects |
| Far mountains | parallax | 800×600 | map-far | `—` | assets/maps/map-far.png | shape(800,180,0x8aa0c8) | Distant silhouette mountains |
| Mid trees | parallax | 800×600 | map-mid | `—` | assets/maps/map-mid.png | shape(800,200,0x4a7c59) | Mid-distance tree canopy |
| Ground/scene base | platform-base | 800×600 | map-ground | `design/crops/gameplay/background.png` only if clean; otherwise `—` + waive crop as reference-only | assets/maps/map-ground.png | color(0x6b4423) | Stable terrain/atmosphere only; never retain foreground actors/UI |
| ... | ... | ... | ... | ... | ... | ... | ... |

**Background Type Guide by Genre**:

- `platform` (incl. auto-scroll variant) / side `beat_em_up` → parallax layers (sky, far, mid, near) via side_scroll_mode
- `bullet_hell` / top `beat_em_up` / real-time `roguelike` → layered raster (ground-only base + separate props)
- `tower_defence + level_model:authored` → terrain-only base; runtime geometry
  comes from `levels/map-layout.json`. Other models follow their topology contract.
- `puzzle` (grid) → tileset-based grid
- `sandbox` / `puzzle` (physics) → baked raster background
- `card` / `idle` → minimal background; focus on UI assets

**Critical**: Backgrounds contain ONLY stable non-interactive scenery. No trees, crates, enemies, platforms, or pickups. Those are props or game objects. A full-mockup `background` crop is only a production seed when it is clean enough to remove foreground actors/UI reliably; otherwise waive that crop as reference-only and generate the §2C base text-first from the mockup's art direction. Other layers may use `Ref Crop = —`.

### 2D: Prop Plan

| Prop | Production | Dimensions | Category / Flags | Key | Ref Crop | Runtime Path | Placeholder | Description |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Rock | Individual | 96×72 | prop; collidable:true | prop-rock | `design/crops/gameplay/rock.png` | assets/props/prop-rock.png | shape(64,48,0x777777) | Compact collidable rock |
| Bush | Individual | 96×64 | prop; decorative:true | prop-bush | `—` | assets/props/prop-bush.png | shape(80,56,0x448844) | Decorative foliage |
| Large tree | Individual | 160×288 | prop; collidable:true | prop-large-tree | `design/crops/gameplay/tree.png` | assets/props/prop-large-tree.png | shape(128,256,0x336633) | Tall irregular obstacle |
| Platform left cap | Individual set member | 64×32 | tileset; collidable:true | tile-platform-left | `—` | assets/tiles/tile-platform-left.png | shape(64,32,0x886644) | Left edge |
| Platform seamless middle | Individual set member | 64×32 | tileset; collidable:true; seamless:true | tile-platform-mid | `—` | assets/tiles/tile-platform-mid.png | shape(64,32,0x886644) | Horizontally tileable middle |
| Platform right cap | Individual set member | 64×32 | tileset; collidable:true | tile-platform-right | `—` | assets/tiles/tile-platform-right.png | shape(64,32,0x886644) | Right edge |
| ... | ... | ... | ... | ... | ... | ... | ... | ... |

Each `generated-image` row is one `produce_static_asset` call. Supplied,
licensed, and procedural rows use their declared integration path.

### 2E: UI Element Registry (the ONE table per UI element — art + size + binding)

**This is the single per-element home** for each declared UI/HUD visual:
source strategy, profile sizing contract, binding flag, and optional Ref Crop.
Positions live in §2.0.2; value/slot anchors live in §2E.1.

Under `art_directed`, fixed lettering and functional widgets are image assets
and every grounded crop is promoted or waived. Under other profiles, only
`required:true` crops are obligations; live/localized text or deliberate
composition may be the declared final strategy. Dynamic values are always
runtime-bound and never baked.

**Sizing follows §2.0.1.** Use `native:<dimension>`,
`fit:<logical box>`, or `base:<pixels>; scales:[integers]` according to profile.
Never infer it from `box_px`.

**Source strategy combines provenance + rendering:** `generated-image`,
`supplied-image`, `licensed-image`, `procedural`, `live-text`, or `composition`.
`bitmap-text` is unsupported until a complete manifest-backed font-atlas
contract exists. `art_directed` functional widgets use image strategies.
Assets-agent generates only `generated-image` rows. Once any image key is
declared, runtime must wire it.

`supplied-image`/`licensed-image` require an existing input path and provenance
(including licenseId for licensed work). Existing stills use the registration
route; existing animated sprites are unsupported. Never select them speculatively.

**Continuous-value widgets:** under `art_directed`, plan frame + fill assets:
- `ui-<name>-frame` (or `ui-<name>-bg`): static chrome/frame/ticks/icons/STATIC label, value-less anchors for any dynamic number.
- `ui-<name>-fill`: shaped fill art only (liquid/gradient/glow/highlight), no text; implement-agent reveals it at runtime via crop/mask/tint driven by the live value.
Other profiles may declare a composited fill, but its live value/binding and
visual bounds remain contractual.

| Element | Type | Source strategy | Purpose | Sizing contract | Binding | Key | Ref Crop | Runtime Path | Placeholder | Description |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Title logo (wordmark) | lettering | generated-image | TitleScene | native:h102 | — | ui-title-logo | `design/crops/title/game-title-text.png` | assets/ui/ui-title-logo.png | shape(360,102,0xffffff) | Art-directed example; other profiles may declare live text |
| Start prompt (styled) | lettering | generated-image | TitleScene | native:h72 | — | ui-start-prompt | `design/crops/title/tap-to-start-text.png` | assets/ui/ui-start-prompt.png | shape(260,72,0xffffff) | Decorative lettering example |
| Game-over word | lettering | generated-image | GameOverScene | native:h160 | — | ui-gameover-word | `design/crops/gameover/game-over-text.png` | assets/ui/ui-gameover-word.png | shape(420,160,0xff4444) | Decorative lettering example |
| Start button | widget | generated-image | TitleScene | native:h64 | — | ui-btn-start | `design/crops/title/start_button.png` | assets/ui/ui-btn-start.png | shape(180,64,0x4466aa) | Art-directed button example |
| Health bar Frame | widget | generated-image | GameScene HUD | native:h24 | §2E.1 | ui-health-frame | `design/crops/gameplay/health_bar_frame.png` | assets/ui/ui-health-frame.png | shape(220,24,0x333333) | Frame with dynamic HP anchor |
| Health bar Fill | widget | generated-image | GameScene HUD | native:h16 | — | ui-health-fill | `design/crops/gameplay/health_fill.png` | assets/ui/ui-health-fill.png | shape(210,16,0xdd3333) | Live fill example |
| Timer panel | widget | composition | GameScene HUD | fit:160×56 | §2E.1 | ui-timer-panel | `—` | runtime | shape(140,48,0x222222) | Responsive/minimal composition example |
| ... | ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |

(Rows are illustrative. Apply the selected profile: the all-static-text-as-art
and frame+fill requirements are `art_directed` policy, not universal rules.)

#### 2E.1 Binding Spec — where each runtime value sits + what each slot does

Any image or composed widget with dynamic values/slots is a **binding visual**.
Its source alone does not define value homes or actions, so author anchors/slots
here from the reference or explicit layout contract.

| Field | Meaning | Measured from |
| --- | --- | --- |
| `value_less: true` | the frame bakes NO numbers/labels — they are overlaid at runtime | — |
| `anchors[]` | one per overlaid value: `{ id, binds, xPct, yPct, fontPct, align }` | the value's measured pixel rectangle **relative to the asset crop rectangle** |
| `slots` | interactive sub-regions: explicit `[{ id, xPct, yPct, wPct, hPct, action }]` (fixed buttons) **or** data-driven `{ source: "CONFIG.<roster>", action }` (one slot per roster entry) | each slot's measured pixel rectangle relative to the asset crop |

- `xPct`/`yPct` = the value/slot **center as a fraction of the asset box** (0–1), so it tracks the asset when scaled to its measured display size. `fontPct` = text height ÷ asset height (the number is sized for the gauge, not a guessed `28px`). `align` ∈ `left|center|right`. `binds` = the CONFIG path / runtime field the text shows (`CONFIG.towers[].cost`, `score`, `wave`, `energy`).
- **Measure, don't guess.** Ground the sub-element (the number area, each card region) like any crop, then `xPct = (sub.x + sub.w/2 − asset.x) / asset.w`, `yPct` likewise, `fontPct = sub.h / asset.h`.
- **Slot count is data-driven.** A card panel/toolbar's slot count is `source`'s size, never a hardcoded literal — so art, code, and function stay in lockstep. Fixed UI bars (pause/settings/restart) use the explicit `slots[]` form, one entry per button with its `action`.
- `gateUiBinding` blocks missing anchors/slots, values not rendered as live
  text, slot count drift from its source, and unwired actions.
- `gateHudBinding` (tower-defence-specific) additionally enforces: a value-less
  tower panel, a `src/` reference to `CONFIG.towers`, live cost text, and
  legacy numeric slot↔roster parity when such a count is declared. Data-driven
  source/roster parity and other live labels are enforced by `gateUiBinding`.
  See `asset-plan-guide.md` "Binding Assets" for the full binding contract.

Example (a gauge HUD + a tower card panel + a fixed control bar):

```
ui-energy-gauge  value_less: true
  anchors: [ { id:"pct",  binds:"energy", xPct:0.50, yPct:0.46, fontPct:0.34, align:"center" } ]
ui-tower-panel   value_less: true
  anchors: [ { id:"name", binds:"CONFIG.towers[].name", xPct:0.5, yPct:0.18, fontPct:0.12, align:"center" },
             { id:"cost", binds:"CONFIG.towers[].cost", xPct:0.5, yPct:0.86, fontPct:0.14, align:"center" } ]
  slots:   { source:"CONFIG.towers", action:"selectTower" }   # slot count == #towers, laid out from the roster
ui-control-bar   value_less: true
  slots:   [ { id:"pause",   xPct:0.20, yPct:0.5, wPct:0.30, hPct:0.8, action:"togglePause" },
             { id:"restart", xPct:0.80, yPct:0.5, wPct:0.30, hPct:0.8, action:"restartGame" } ]
```

---

## Section 3: Game Configuration

Write the COMPLETE config object with exact values. Gameplay/tuning values are
authored, never lifted from mockup numerals. `CONFIG.ui` always carries
positions and bindings. It additionally carries a logical fit box for
responsive/minimal or allowed integer scales for pixel art; art-directed image
dimensions remain asset-native.

```javascript
export const CONFIG = {
  screen: { width: ___, height: ___ },  // = §1 resolution = §2.0 mockup canvas
  physics: { gravity: ___, debug: false },
  player: {
    speed: 200,          // feels responsive without losing control
    jumpForce: -400,     // ~0.8s airtime at gravity 300
    maxHealth: 100,
    attackDamage: 25,    // 2-shot kills basic enemy (health 30 + overkill)
    invulnerableTime: 1000
  },
  enemies: {
    basic: { speed: 80, health: 30, damage: 15, score: 10 },
    fast: { speed: 150, health: 15, damage: 10, score: 15 }
  },
  spawning: { rate: 2000, maxCount: 10 },
  scoring: { enemyKill: 10, coinPickup: 5 },
  levels: { count: 1, width: 1600 },
  ui: {
    // Position is always derived from §2.0.2. Add fit/base-scale fields only
    // when the selected quality profile authorizes runtime uniform scaling.
    healthBar: { x: 20, y: 20 },        // origin set in code; size = ui-health-bg PNG
    startButton: { x: 640, y: 520 },    // size = ui-btn-start PNG
    score: { x: -20, y: 20, fontSize: 28 },  // runtime text: negative x = offset from right edge; fontSize ≈ 2.5% of canvas h
    // Binding assets (§2E.1) carry anchors/slots so text lands ON the asset and
    // slot count/function come from the spec — NOT free-floating screen coords.
    // anchors are FRACTIONS of the asset box, so they need no width/height here.
    energyGauge: {
      x: 640, y: 40, key: 'ui-energy-gauge', value_less: true,
      anchors: [ { id: 'pct', binds: 'energy', xPct: 0.5, yPct: 0.46, fontPct: 0.34, align: 'center' } ],
    },
    towerPanel: {
      x: 640, y: 680, key: 'ui-tower-panel', value_less: true,
      anchors: [
        { id: 'name', binds: 'CONFIG.towers[].name', xPct: 0.5, yPct: 0.18, fontPct: 0.12, align: 'center' },
        { id: 'cost', binds: 'CONFIG.towers[].cost', xPct: 0.5, yPct: 0.86, fontPct: 0.14, align: 'center' },
      ],
      slots: { source: 'CONFIG.towers', action: 'selectTower' },  // one card per tower — count is data-driven
    },
  }
};
```

---

## Section 4: Entity & Scene Architecture

### Per Scene:

**[SceneName]** (extends Phaser.Scene)

- **Purpose**: [what happens in this scene]
- **Concept ref**: [the §2.0 mockup this scene realizes, e.g. `design/gameplay.png`] — match its §7 Concept-Derived Layout
- **Entities**: [list game objects with properties]
- **Animations used**: [list animation keys from Section 2 asset tables]
- **Interactions**: [collisions, overlaps, triggers — must honor §0.2 rules]
- **State**: [variables managed: score, health, timer, etc.]
- **Exit conditions**: [what triggers scene transition]

### Per Entity:

**[EntityName]**

- **Visual**: Key=[key from Section 2], Frame Size=[w×h]
- **Hit shape**: layer + `{shape, body, fit:bbox}` — authored once in §0.2's per-entity hit shape (do NOT restate a `size [w,h]` here; the body size is MEASURED from `contentBbox` at integrate and lands in `display-params.json`). Name the §0.2 layer this entity belongs to.
- **Properties**: speed=[n], health=[n], damage=[n] (all from CONFIG)
- **Behavior**: [movement pattern, AI logic — must match §0.2 Entity Behaviors; do not add behavior absent from the mechanics contract]
- **Animations**: idle, run, attack, death (must match actions listed in Section 2A for this entity)
- **Events**: on collision with [X] → [effect + feedback from Section 5]

---

## Section 5: Game Feel & Juice

Every player action and game event needs immediate, visible feedback. This section is the contract for all feedback effects implemented by implement-agent.

### Feedback Table

| Event        | Visual Feedback                            | Tween/Effect                                 | Duration |
| ------------ | ------------------------------------------ | -------------------------------------------- | -------- |
| Player jump  | Squash on launch, stretch in air           | scaleX:1.2→1, scaleY:0.8→1 over 100ms        | 100ms    |
| Player land  | Brief squash                               | scaleX:1.3, scaleY:0.7 → restore over 150ms  | 150ms    |
| Enemy hit    | Flash white, knockback                     | tint 0xffffff for 100ms, dx ±80              | 100ms    |
| Enemy death  | Flash + runtime death poof                 | `recipes.explosion(...)`; destroy after body animation | 400ms |
| Player hurt  | Screen flash red, brief shake              | camera shake 50ms intensity 0.01             | 50ms     |
| Coin collect | Runtime glow + score +1 float text         | `recipes.pickupGlow(scene, coin, opts)`; text floats and fades | 500ms |
| Boss appear  | Screen slow-zoom, brief freeze             | time.scale 0.3 for 300ms, camera zoom 1→1.05 | 300ms    |
| ...          | ...                                        | ...                                          | ...      |

### Effects Strategy

Declare one strategy per effect: particle recipe, flipbook, mask/shader, or
none. `art_directed` defaults to runtime particle recipes; other profiles follow
their profile/GDD declaration. Include trigger, cleanup/lifetime, and parameters.

---

## Section 6: Level & Content Design

Branch only on `level_model`:
- `authored`: this section contracts the Level DSL and level-design-agent;
- `procedural`: specify generation rules, seeds, constraints, and validation;
- `board`: specify board/deck/object data and transitions;
- `none`: state why no staged content model is needed.

### Stage Outline (`level_model: authored` only)

| # | Stage Name | Theme / Setting | New Element (mandatory for stage 2+) | Boss | Target Length |
|---|------------|-----------------|--------------------------------------|------|---------------|
| 1 | [name]     | [biome]         | [baseline verbs / enemies]           | [id] | [mm:ss]       |
| 2 | ...        | ...             | [new mechanic]                       | ...  | ...           |

The "New Element" column ensures every stage beyond 1 escalates Scene + Mechanic + Difficulty. Rows that cannot fill "New Element" must be dropped. Stage count and per-row content are converted 1:1 into `levels/stage_N.json` by level-design-agent.

### Content Specification

Content details are genre-specific *inside* the selected model; genre never
decides whether `levels/*.json` exists.

**platform (incl. auto-scroll variant) / side beat_em_up**: Platform layout, enemy placement, collectible positions, section pacing
**bullet_hell / top beat_em_up / real-time roguelike**: Arena bounds, spawn points, patrol routes, obstacle positions, bullet-pattern / wave schedule
**puzzle (grid roguelike)**: Grid dimensions, initial state, win condition, valid moves
**tower_defence + authored**: exact path/core/spawn/build-slot geometry in the
Level DSL. **tower_defence + procedural/board**: specify that model's
authoritative path/topology data instead; do not fabricate `levels/map-layout.json`.
**sandbox / puzzle (physics)**: Object types, launch mechanics, target structures
**simulation**: System entities, economy/resource rules, tick behavior, milestone targets
**card / idle**: Card decks / upgrade trees, number curves, unlock schedule
**custom**: Describe the content structure explicitly

### Difficulty Curve

| Phase      | Time / Wave | Challenge Level | New Element Introduced       | Player Emotion     |
| ---------- | ----------- | --------------- | ---------------------------- | ------------------ |
| Tutorial   | 0:00–0:30   | Low             | Basic movement + first enemy | Learning           |
| Build-up   | 0:30–2:00   | Medium          | Enemy variety + collectibles | Growing confidence |
| Challenge  | 2:00–4:00   | High            | Fast enemies + hazards       | Tension            |
| Climax     | 4:00–5:00   | Peak            | Boss or survival wave        | Adrenaline         |
| Resolution | win/lose    | —               | Final score + replay prompt  | Satisfaction       |

---

## Section 7: UI & HUD Design

When a scene has a mockup, embed and derive its layout from that reference.
For shared scenes, link the shared reference and author the normalized region
contract. For a concept-generation-waived scene, state the exact waiver token and
author the normalized region contract without claiming that its positions were
measured from an image.

> **§7 is a placement view derived from §2 + §2.0.2.** Every image key
> must already exist in §2 and the manifest. Live text/composed primitives are
> also declared in §2E when the profile authorizes them. Dynamic values remain
> runtime-bound. §7 never invents an undeclared visual source.

```markdown
#### [SceneName] — Concept-Derived Layout

![[scene] concept](design/[screen].png)

- **Composition**: [overall framing / what dominates the screen, read from the image]
- **Regions & elements**: [each visible element + WHERE it sits — corner/edge/center, anchor]
- **HUD**: [gauges/counters/buttons and their exact corners/edges, as drawn]
- **Palette**: [the dominant colors actually used in this mockup]
```

Then the concrete layout tables/specs below. Positions derive from §2.0.2.
Profile sizing fields follow §2.0.1: native art stays asset-side; logical fit
boxes or integer scale sets may live in `CONFIG.ui` for profiles that use them.

### TitleScene Layout

- Game title: use its §2E source strategy (art-directed image; polished
  live text is allowed by other profiles).
- Subtitle/instruction: use its declared §2E source and placement.
- Start trigger: [key or click]

> Text follows `QUALITY_PROFILE`. Dynamic values are always live and updated;
> fixed copy is image art only under `art_directed`.

### GameScene HUD Layout

This is a placement view; source and sizing remain in §2E. Place images,
live text, and deliberate compositions exactly as that registry declares.
Dynamic values are runtime-bound.

| Element    | Position            | Anchor        | Asset (§2E key)        | Description                      |
| ---------- | ------------------- | ------------- | ---------------------- | -------------------------------- |
| Health bar | top-left (20, 20)   | origin(0,0)   | §2E source | frame/fill image or declared composition |
| Score      | top-right (-20, 20) | origin(1,0)   | runtime `add.text`     | "Score: 0", fontSize from CONFIG (dynamic value) |
| Timer      | top-center          | origin(0.5,0) | runtime `add.text`     | "0:00" countdown (if applicable) |
| ...        | ...                 | ...           | ...                    | ...                              |

HUD elements use fixed camera/depth policy. Positions derive from §2.0.2; sizing
uses each element's native, logical-fit, or integer-scale §2E contract.

> **Proportion contract.** Gates compare actual rendered bounds with the
> profile sizing contract and reject aspect drift, spill, overlap, or stale
> recorded dimensions.

> **Binding contract (text-on-asset + slot/function parity).** For every widget that overlays a value or has multiple slots, its §2E.1 Binding Spec (`anchors`/`slots`) is authoritative: each overlaid number/label is placed at its anchor (a fraction of the asset box, NOT a free-floating screen coordinate), and each slot's count + `action` come from the spec. `CONFIG.ui.<key>.anchors`/`.slots` carry these into code; the implementer uses `placeUiText`/`bindUiSlots` so text lands on the asset and the button count/function match it.

### GameOverScene Layout

- Result word, score host, and restart control use their §2E source strategies.
  Changing values remain live and bound; art-directed fixed labels may be baked.
- Restart trigger: [key]

---

## Section 8: Implementation Roadmap

Numbered list of exact file operations:

1. CREATE `src/config.js` — export CONFIG from Section 3
2. CREATE `src/scenes/BootScene.js` — preload all assets, show loading bar
3. CREATE `src/scenes/TitleScene.js` — per Section 7 TitleScene layout + concept
4. CREATE `src/scenes/GameScene.js` — main gameplay per Section 4 + §0.2 mechanics
5. CREATE `src/scenes/GameOverScene.js` — per Section 7 GameOverScene layout
6. UPDATE `src/main.js` — import all scenes, set physics config per Section 1
7. IMPLEMENT feedback effects per Section 5 in GameScene
8. VERIFY — open in browser, test full loop (double-axis: looks like §2.0, plays like §0.2)

---

## Forbidden in GDD

- **Isometric / 3D-perspective framing** — mockups, §7 layouts, or asset art drawn in isometric/axonometric projection, or with 3D perspective / vanishing points / a tilted horizon / a receding ground plane, or objects extruded into perspective 3D boxes. This is a **flat-2D-view** pipeline (§1 Projection); render any depth the idea implies (tumbling dominoes, "3D ramps", stacked blocks) as a flat side-on or top-down 2D view. (This forbids 3D *projection*, NOT shaded/volumetric *finish* — sprites are still richly rendered per §2 Art Style.)
- Unspecified values ("appropriate amount", "some damage", "a few enemies")
- Static sprites for characters or enemies — every entity needs animation
- Code snippets (this is design, not implementation)
- Scope beyond a single demo session
- Missing animation specs — every entity action must have a row in Section 2A/2B
- Audio/sound design (out of scope — this pipeline does not produce audio)
- Features that contradict explicit Prompt requirements or exclusions
- Missing runtime paths / placeholders in Section 2 — scaffold must be able to build `assets/asset-manifest.json`
- **Generation prompts or parameters** — assets-agent decides how to generate
- **Frame counts or grid layouts** — determined by i2v video output, not pre-specified
- **Post-processing instructions** — assets-agent owns the production pipeline
- **Untagged or contradictory mechanics** — concept-led and genre-default
  additions are allowed only where the Prompt is silent; tag their source and
  never override explicit Prompt requirements or exclusions
- **Numerals lifted from a mockup** — placeholder HUD numbers in an image are not design values; §3 CONFIG is authored, not read off the picture
- **Copying `box_px` as a UI size** — `box_px` is a loose crop frame, not a size. Author each widget's Target size from its role + the mockup's relative look (§2.0.1), locking ONE dimension; never give an off-aspect w×h (the asset's own aspect supplies the other axis). `gateUiProportion`/`aspect-drift` block a squashed asset.
- **Eyeballing a position (or using `box_px` as one)** — typing a `CONFIG.ui`
  x/y or §7 coordinate by guessing from the mockup, or reusing a crop's
  `box_px` as a position. Positions are MEASURED by `detect_layout` and designed
  into §2.0.2; `CONFIG.ui` x/y + §7 then DERIVE from §2.0.2.
  `gateLayoutAlignment` applies the kind-aware center/containment/declared-origin
  checks above and blocks a rendered element that drifts from its §2.0.2 `box`;
  `gateUiLayout` owns composite child containment/overlap/overflow.
- **A §2A–2E element with no §2.0.2 row** — every UI element, entity, prop, and
  decorative piece that appears on a scene needs a §2.0.2 layout row (its
  measured `box` + `origin` + `anchorTo` + `kind`). A missing row is an
  un-verified placement; `gateLayoutAlignment` flags the coverage gap using the
  same kind-aware rules.
- **Functional widget as a placeholder** — planning a health/energy bar, button, panel, badge, or loading bar as `add.rectangle`/`add.graphics`/bare `add.text` instead of a generated art asset, OR waiving its crop (`gateUiArtParity` blocks both)
- **Value-less asset with no Binding Spec** — a widget that overlays a number/label or has multiple slots but no §2E.1 `anchors`/`slots` (its text would float off the asset / its slot count would be a guess; `gateUiBinding` blocks)
- **Guessed text position / font size on an asset** — an overlaid value placed by a free-floating screen coordinate or a `28px` guess instead of a measured anchor (`xPct/yPct/fontPct` relative to the asset box)
- **Hardcoded slot/button count** — a card panel/toolbar count typed as a literal instead of derived from its `CONFIG.<roster>` source, or a slot whose `action` is undefined (`gateUiBinding`)

---

## Visual Quality Rules

Technical parameters must support the intended visual quality. Common mistakes:

- **Flat *view* mistaken for flat *shading*** — the flat 2D constraint (§1/§2) is a **camera/projection** rule (straight side-on or top-down so the engine can place sprites), NOT a finish rule. A correctly-2D sprite that is *also* flat-colored looks like programmer art; render it with volume, soft/gradient shading, highlights, and material per §2 Art Style. Reserve genuinely flat fills for explicitly flat/vector art styles and for tiles that must tile seamlessly.
- **Grid/cell sizes too large** — if the game uses a grid or cellular simulation, the cell size must be small enough that individual cells are not visually obvious. The player should see smooth terrain/fluid, not a grid of blocks.
- **Flat textures** — runtime-generated textures need noise, gradients, and edge detail. Solid-color rectangles look like programmer art.
- **No depth** — backgrounds need visual layers (far, mid, near), not a single flat color.
- **Accidental raw UI** — reject scaffold fallback and incoherent default styling.
  Profile-authorized composition/live text is valid when it is deliberate,
  consistent, readable, and declared in §2E.

When choosing numeric parameters (grid resolution, particle counts, animation frame rates), ask: "If I screenshot this, would it look like a finished game or a prototype?"
