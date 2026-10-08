# Concept Art Guide — Profile-Aware Visual Reference for the GDD

This is the full image-first procedure for `art_directed`. For `responsive`,
`pixel_art`, and `minimal`, own or explicitly shared references are valid.
All profiles must attempt concept mockup generation for the multimodal GDD.
Whenever a reference exists, derive visual composition from it.
If bounded generation fails, preserve the full design with the exact
`waived:concept-generation-unavailable` reference fallback described below.

Steps (full phase breakdown in the design-agent's §5 Workflow):

1. **Coverage plan** — list the conventional screens to draw (title, ONE gameplay, level-select, menu/overlay, win/lose). Read concept-image limits from the dispatched `CONFIG_PATH`.
2. **Size-accurate references** — draw every required own reference at one canvas.
3. **Author from the images** — `Read` one representative mockup for shared
   style/palette and use each screen's `detect_layout` result as its structured
   composition read-back. `Read` an additional mockup only when its visual style
   is materially different or detection leaves an ambiguity. Then write the GDD
   prose (§0–8). This is Phase 3.
4. **Grounding** — crop contracted elements; outside `art_directed`, mark each
   promoted crop `required:true`.
5. **Embed + drive** — embed references/layouts and map crops to contracted rows;
   generated-image rows use them as generation seeds.

## What this is (and is NOT)

- **IS**: opaque full-scene **mockups** + **grounded element crops** that (a) document the intended look/layout, (b) **seed** asset generation, and (c) **source the GDD prose** — §1 resolution, §2 art style, §7 layout, and the gameplay described in §0/§4 are all written FROM these mockups (image-first).
- **IS NOT**: assets. Everything here lives under `design/`, is never loaded by
  Phaser, and never counts as runtime art. `generated-image` rows use crops as
  seeds; supplied/licensed/procedural/live strategies follow their own source.
- Because they are references, **quality bar is low**: rough, loosely-cropped, or imperfect mockups/crops are acceptable — they just convey look + layout. Don't iterate on them; the real asset is regenerated downstream.

## Prerequisite: open workspace

The orchestrator binds `GAME_DIR` before design dispatch. Concept tools resolve
relative paths there; design-agent must not reinitialize it.

## Layout on disk (doc §8 convention)

```
playground/<game-slug>/design/
  <screen>.png                 # size-accurate full-scene mockups (one per screen)
  <screen>.prompt.txt
  crops/
    <screen>/<element>.png     # grounded per-element reference sub-images
    <screen>/crops.json        # grounding manifest
```

Use `design/` — never `assets/` (the asset-integrity hook rejects reference docs placed under `assets/`).

---

## Step 1 — Scene Coverage Plan (do this FIRST)

Coverage is scene-driven. Assign every scene `own`, `shared:<reference>`,
`waived:pure-transition`, or `waived:concept-generation-unavailable`.
Interactive `art_directed` scenes require `own`; responsive/pixel/minimal may
share explicitly.
After bounded generation failure, the affected scene may use only
`waived:concept-generation-unavailable`.

**Name every mockup by its scene-key slug** — drop the `Scene` suffix, kebab-case the rest. verify auto-maps scene→mockup on the *normalized* name (`Scene` stripped, non-alphanumerics removed), so a slug-named mockup is picked up with **NO `--map`**; a generically-named one misses the name match and falls back to unstable box-overlap (and may end up unmapped = unverified).

| Scene (§1 registry) | Mockup key (slug) | Coverage | Becomes (§7 scene) |
|--------------------|-------------------|----------|----------|
| `MainMenuScene` | `main-menu` → `design/main-menu.png` | own | MainMenuScene |
| `ModeSelectScene` | `mode-select` → `design/mode-select.png` | own | ModeSelectScene |
| `LevelSelectScene` | `level-select` → `design/level-select.png` | own | LevelSelectScene |
| `MatchScene` (gameplay) | `match` → `design/match.png` | own | MatchScene |
| *(game-over overlay)* | `gameover` → `design/gameover.png` | own | overlay on MatchScene |
| `SplashScene` | — | `waived:pure-transition` (logo only, no interactive UI) | SplashScene |

Rules:

- **Every interactive scene gets its profile-required reference attempt.** Only
  the two exact waiver tokens above are valid. The bounded-failure token exempts
  only the image: author the complete normalized layout, mechanics, assets, and
  UI, and return a warning.
- **ONE gameplay mockup per representative gameplay scene** (not one per biome/stage — note biome palette differences in §2 text). A distinct **overlay state** drawn on top of a gameplay scene (game-over, pause) gets its own mockup keyed by the state name.
- **The configured cap bounds cost, not scene coverage.** Never drop a scene to
  stay under it; mark justified overflow `EXTENDED SCOPE`.
- **Mockups must be mutually consistent.** All screens depict ONE game: same art style, same entity roster (names, counts), and — critically — the SAME map/path layout and the SAME control set wherever they appear. If the title shows energy-rail lanes, the gameplay screen must not show a paved highway; if the panel lists 4 towers named Vanguard/Dread/Frost/Toxin, every screen lists those same 4 (and §3 CONFIG uses those names). Contradictory mockups produce contradictory assets and code downstream — regenerate the outlier rather than letting two truths ship.
- **No separate `lineup` / `props` / `vfx` / `hud` system sheets by default.** The per-element Ref Crops (Step 4) are grounded **out of the scene mockups**. Only add ONE extra `lineup` sheet (within the cap) if key entities are too small/occluded in the gameplay screen to crop cleanly.

---

## Step 2 — Draw every mockup at ONE genre-default canvas

> **FLAT 2D VIEW — never 2.5D / isometric / 3D-perspective (hard rule, all screens).** What's locked is the **view/projection**, not the shading. This pipeline ships **flat 2D Phaser sprite games**, so every mockup MUST read through a flat 2D camera: a **straight side-on** or **straight top-down (bird's-eye)** view. FORBIDDEN in every screen mockup: isometric / axonometric projection, perspective depth / vanishing points / a tilted horizon, a floor plane receding into the distance, objects extruded into perspective 3D boxes, drop shadows that imply height, or any "2.5D" diorama framing. This holds **even when the idea implies depth** — tumbling dominoes, "3D ramps", stacked blocks, a ball rolling "into" the screen: draw the flat 2D way a 2D sprite engine actually renders it (a flat top-down board or a flat side elevation), because an isometric/perspective mockup yields layouts and asset crops the 2D runtime cannot reproduce. The flat-view rule is NOT "flat-colored": objects within the flat silhouette SHOULD be shaded/rendered (volume, soft shading, materials) per the chosen art style, since these mockups seed the final assets — **depth is painted via shading, never projected via perspective.** Put `flat 2D [view]` (and the chosen view) in EVERY mockup prompt; if a render comes back isometric/3D-perspective, **regenerate it** (the flat-VIEW constraint is locked, the finish is not).

> **How to actually GET a flat *view* (gpt-image-2 fights you on inherently-3D subjects).** A trailing "no 3D" line is not enough — the model amplifies whatever the prompt *emphasizes*. Two reinforcements:
> 1. **Tool-layer (automatic):** `mcp__gca__generate_image` auto-injects a HARD flat-VIEW constraint block for any `output` under `design/` (and turns off the single-subject margin so the full scene is allowed). You don't pass anything — but it only works if the prompt body doesn't fight it.
> 2. **Prompt body (your job):** **Describe every object in its FLAT-VIEW form** and **avoid 3D-*projection* trigger words** (keep finish words):
>    - Top-down: a domino → "a white rounded-rectangle tile with black pip dots, seen from straight above, softly shaded with no perspective thickness"; a ramp → "a shaded trapezoid seen flat-on"; a bell → "a top-down bell icon, shaded". Side-on: draw the flat side silhouette, shaded for volume.
>    - **Forbidden — they drag the render into a 3D *projection*** (regenerate if they slip in): `3D`, `isometric`, `axonometric`, `perspective`, `vanishing point`, `receding / into the distance`, `standing/upright`, `tumbling/rolling into the screen`, long `cast shadow`. (Also avoid `photoreal/photographic` — stylized, not photographic.)
>    - **Allowed — finish words that keep the flat view but add polish** (use them when the art style wants refinement): `flat-view`, `top-down`, `side-on`, `2D game icon/tile`, **and** `shaded`, `soft shading`, `gradient shading`, `volume`, `rendered`, `materials`, `rim light`, `highlights`. Reserve `flat-shaded` / `flat vector` for explicitly flat/vector art styles.

There is no §1 Resolution yet — pick the canvas from the **genre** and draw all screens at it. That canvas then *becomes* §1 Resolution. `gpt-image-2` takes **flexible** sizes and `normalize_size` snaps each dim to a multiple of 16 in the legal range (aspect preserved), so you get the chosen aspect with **no cropping**. Do NOT generate `1536x1024` then crop to 16:9 — that loses the HUD.

| Genre | Default canvas |
|-----------|----------------|
| platform / beat_em_up / bullet_hell / tower_defence / sandbox | `1280x720` (16:9 landscape) |
| roguelike | `1024x768` (4:3 landscape) |
| puzzle | `800x800` (square) |
| card / idle (mobile-style) | `720x1280` (portrait) |
| simulation / custom | pick the aspect that matches the gameplay |

Pick ONE and reuse it for every screen so §1 stays consistent.

```jsonc
mcp__gca__generate_image({
  prompt: "Full-screen MOCKUP of [screen] for [idea], flat 2D [genre] game, [straight top-down bird's-eye | straight side-on] view, polished 2D game art in [the chosen art style] (shaded, with volume and materials — not flat-vector unless the style is flat). [Describe each on-screen object in its FLAT-VIEW form — e.g. 'dominoes as white rounded-rectangle tiles with black pip dots seen from straight above, softly shaded but with no perspective thickness / receding side-faces / height shadow', 'shaded trapezoid ramps seen flat-on', 'a top-down bell icon at the goal']. Plausible HUD + on-screen elements. Opaque full-scene, no transparency, no magenta. Placeholder text blocks only.",
  output: "design/gameplay.png",   // under design/ → tool auto-injects the hard FLAT-2D block
  size: "1280x720",          // the genre-default canvas (snapped to /16); becomes §1
  quality: "medium"
})
```

> Note the example body has NO `3D` / `perspective` / `standing` / long-`cast shadow` words and spells out the flat-view shape of each object (while still allowing shading) — that, plus the auto-injected flat-VIEW block, is what keeps gpt-image-2 from rendering a 3D-perspective scene without forcing the art flat-colored.

If a mockup reads as the wrong genre or projection, regenerate within the
tool's normal bounded attempts. If those attempts fail, use
`waived:concept-generation-unavailable`, record the actual cause as a warning,
and continue; never retry forever or block the design phase. Only for an EXACT
odd resolution that isn't a multiple of 16 (e.g. `960x540`) call
`mcp__gca__fit_image_to_resolution` — it defaults to `mode: "contain"` and
**never crops content**:

```jsonc
mcp__gca__fit_image_to_resolution({ input: "design/gameplay.png", output: "design/gameplay.png", width: 960, height: 540 })
```

Save each prompt to `design/<key>.prompt.txt`.

---

## Step 3 — Author the GDD prose FROM the mockups (read-back → write)

Now that the screens exist, write the GDD text to *describe them*. `Read` one
representative mockup for the shared style, palette, mood, and material treatment.
For every screen, form an **independent** composition description from its
generation prompt plus canonical `detect_layout` output. Do not spend duplicate
tool calls reading visually consistent screens whose structured detection is
complete; `Read` an additional mockup only to resolve a material visual difference
or ambiguity. A useful read-back framing:

```
Read design/<screen>.png and describe ONLY what is actually visible:
- screen identity + overall composition / camera framing
- art style, palette (name the dominant hex-ish colors), mood
- every on-screen element and WHERE it sits (regions, alignment, anchors)
- the HUD: which gauges/counters/buttons, and their corners/edges
- what gameplay this implies (movement model, enemy arrangement, visible systems)
Do NOT invent numbers; report placeholder text (e.g. "SCORE 9999") as placeholder only.
```

Then turn those descriptions into the GDD sections:

| Wrote into | Sourced from the image |
|-----------|------------------------|
| **§1 Resolution** | the chosen canvas / mockup dimensions (record it) |
| **§2 Art Direction** | the mockup's actual style / palette / mood |
| **§2.0.2 Scene Layout Design** | the **measured** position of every element, from `detect_layout` (Step 4d) — the lays-out-like SSOT that §7 prose + `CONFIG.ui` x/y derive from |
| **§7 UI & HUD** | each screen's layout described in prose, anchored to the §2.0.2 positions (its "Concept-Derived Layout" block) |
| **§0 / §4 / §5** | gameplay implied by the prompt and mockup; concepts may create coherent mechanics where the prompt is silent, tagged `Source: concept` |

**Guardrail — VISUAL numbers use authored sizes and measured positions (see
gdd-template preamble); gameplay/content tuning is a separate text-authored
rule.** Split the numbers a mockup implies and treat them correctly:

- **Tuning/content numerals = text-authored.** The image cannot specify tuning values. **Never lift placeholder numerals** from a mockup (HUD "SCORE 9999", "x3", "LV 5", a timer) into §3 CONFIG — CONFIG is designer-authored; the image only informs the *shape* of systems.
- UI sizing is authored from role/profile, never crop bounds. Positions are
  detected when a reference exists; scenes whose concept generation failed
  author normalized anchors explicitly. Both project into §2E/§2.0.2.

**Corollary — a number CONFIG owns must not get FROZEN into a final asset.** The guardrail above keeps mockup numerals out of CONFIG; this corollary keeps CONFIG values out of the shipped raster. Any asset that *renders* a CONFIG fact — a tower/unit selection panel's names and costs, a shop, an ability bar — is a **binding asset**: its final art is generated as value-less styled frames (no names, no numbers), and implement-agent overlays the live values as runtime text from §3 CONFIG (the same static/dynamic split used for scores/timers). A mockup MAY paint placeholder labels to convey layout, but the §2E row for that asset must be marked value-less and those labels are NOT promoted into the final art. Otherwise the panel says "Vanguard / 150" while CONFIG says "Pulse / 50" and nothing reconciles them. (See `asset-plan-guide.md` → "Binding Assets".)

**Tower-defence geometry.** Only `LEVEL_MODEL=authored` projects concrete
core/spawn/path/build-slot coordinates into `levels/map-layout.json`; procedural
or board models declare their own topology authority.

---

## Step 4 — Detect layout, then ground contracted reference crops

Ground every selected element for `art_directed`. Under other profiles, ground
only useful references and mark asset obligations `required:true`; deliberate
live text or composition does not need a synthetic crop. Bitmap text is not a
supported pipeline source until a manifest-backed font-atlas route exists. Run the
position detector first so cropping reuses the same stable key set as §2.0.2;
the separate legacy `elements` mode is migration-only.

```jsonc
mcp__gca__detect_layout({
  input: "design/gameplay.png",
  canvas_width: 1280, canvas_height: 720
})
// → design/gameplay.detect.json

mcp__gca__ground_crop({
  input: "design/gameplay.png",
  output_dir: "design/crops/gameplay",
  detect: "design/gameplay.detect.json",
  add_background: true
})
// → { image, source_sha256, detect, detection_sha256, found, missed,
//     crops:[{label,key,crop,path,sha256,box_px,role,kind,source}],
//     manifest, n_found, n_grounded, n_rejected, background }
```

- `detect_layout` owns the normalized position/key measurement; `ground_crop`
  deterministically converts those boxes into art crops without a second VLM.
- Detection evidence records the workspace-relative mockup path and its SHA-256.
  If the mockup changes, rerun `detect_layout` before `ground_crop`; deterministic
  cropping rejects a detection whose source hash no longer matches. `crops.json`
  binds the resulting crops to both that mockup hash and the exact detection-file
  hash, using only stable workspace-relative paths. If either input changes,
  regenerate the crops rather than reusing the old manifest by pathname.
- Empty or background-only detection is a failed grounding pass;
  `add_background:true` adds coverage after usable foreground grounding and is
  not a waiver.
- The tool writes `design/crops/<screen>/<label>.png` + `crops.json`. Each crop
  entry carries `sha256`, bound to the exact crop PNG, and **`box_px` =
  `[x0,y0,x1,y1]`**, the padded source-mockup rectangle used to create the art
  seed and to clip the matching runtime region in element visual diff. `box_px`
  is **NOT a target size and NOT a scene position**. Element *sizes* are
  AUTHORED in Step 4b; element *scene positions* come from normalized
  `detect_layout.box` (Step 4d). Step 4c may derive within-asset anchor/slot
  ratios from sub-rectangles relative to this crop frame.
- Rough crops are fine; generated-image rows regenerate from them.
- For a missed generated element, use `Ref Crop=—` and text/reference fallback.

---

## Step 4b — Author each UI element's profile sizing

Author every §2E element's in-game sizing contract; never copy `box_px`.

- **Role** = icon / button / bar / gauge / panel / banner (drives the size convention).
- **Sizing** = native dimension, logical fit box, or base pixels + integer scales
  according to the selected profile.

`art_directed` records a native target dimension; responsive/minimal record a
logical fit box; pixel art records base pixels and allowed integer scales.
Every strategy preserves aspect and records actual rendered bounds.

> Font sizes: a styled label's font height comes from its Target size; a plain runtime counter's `fontSize` is a sane fraction of canvas height (~2–3%), recorded in `CONFIG.ui`, not an arbitrary `'16px'`.

---

## Step 4c — Measure the Binding Spec for value-bearing widgets (anchors + slots)

A widget that overlays a **dynamic value** (a gauge number, a button label, a card's name/cost) or has **multiple interactive slots** (a card panel, a toolbar) needs more than its outer `box_px`: it needs *where on the asset* each value sits and *how many* slots there are. The mockup already shows this — measure it, don't let the implementer guess (guessing is exactly why "the number floats off the gauge / the label isn't centered on the button").

For each such widget, ground its **sub-elements** too (the number area inside the gauge, each card region inside the panel) so each gets its own `box_px`, then convert to **fractions of the parent asset's box**:

```
xPct  = (sub.x + sub.w/2 − asset.x) / asset.w     # value/slot center, 0–1 across the asset
yPct  = (sub.y + sub.h/2 − asset.y) / asset.h
fontPct = sub.h / asset.h                          # text height relative to the asset (sizes the number for the gauge)
```

Record these into the GDD **§2E.1 Binding Spec** as `anchors[]` (one per overlaid value: `{ id, binds, xPct, yPct, fontPct, align }`) and, for multi-slot widgets, a `slots` block (data-driven `{ source: "CONFIG.<roster>", action }` for repeated cards, or an explicit `[{ id, xPct, yPct, wPct, hPct, action }]` for fixed control buttons). Fractions (not absolute px) so the value tracks the asset when it's scaled to its measured display size. `gateUiBinding` then enforces these are honored in code, and the VLM element axis confirms the number visually lands at its anchor + the visible slot count/function matches.

> Repeated slots (cards) don't need per-card grounding — ground ONE representative card to get its inner anchors (name/cost positions as fractions of a card), then declare `slots: { source, action }`; the implementer lays out `source`-many copies. The slot **count** is the roster size, never a number you type.

---

## Step 4d — Author the §2.0.2 Scene Layout Design from detection (MANDATORY)

Positions are **measured, not eyeballed.** Reuse the
`design/<scene>.detect.json` written at the start of Step 4. `ground_crop`
grabs padded art crops; its `box_px` is the wrong source for *where* an element
sits.

```jsonc
// design/gameplay.detect.json:
// { scene, source:"design/gameplay.png", source_sha256, canvas,
//   n_detected, n_rejected,
//   elements: [{ key, role, box:[x,y,w,h] (0..1), box_canvas_px,
//                center, origin, anchorTo, kind }] }
```

`detect_layout` returns a TIGHT, canvas-normalized box for EVERY element (UI widgets, lettering, actors, props, decorative regions). Then **author** the GDD **§2.0.2 Scene Layout Design** from it — detection informs, you design:

`box_canvas_px` is only a human-readable `[x,y,w,h]` projection onto the
authored canvas. It is distinct from `crops.json.box_px`
(`[x0,y0,x1,y1]` on the source mockup), which element visual diff consumes as
its reference/runtime clipping frame.

- **One block per scene, one row per element**: `key` (the §2A/2D/2E asset/object key), `role`, `box` (normalized `[x,y,w,h]`), `origin`, `anchorTo` (the edge/corner it pins to), and `kind`.
- **Design, don't transcribe**: snap a corner HUD widget to its corner with a clean margin, align a row of buttons, resolve detector overlaps, and round jittery boxes — a slightly-off detection must NOT ship a crooked layout. The detection is the *measurement*; §2.0.2 is the *authored contract*.
- **Mark `kind`**: `static` for fixed UI/decor (center point checked within the
  role-scaled layout tolerance); `spawn` for an entity's initial position (the
  same center-point check with `1.25×` tolerance — there is no special
  spawn-frame capture mode); `zone` for a region the game fills (enemy formation
  area, lane band — the gate checks containment, not a point); `cluster` for a
  **UI asset group / Container** (below).
- **Group spatially-adjacent elements into a `kind: cluster` (Container).** When 2+ elements sit adjacent AND read as one component (a stat-slot = icon+label+value, a meter = frame+fill+number, a results panel, a toolbar, a card), declare ONE cluster row + a `children` block instead of N absolute boxes. Children are BOTH **image children** (`kind: image` → a §2E key) AND **pure-text children** (`kind: text` → `bind` + `maxChars`) — the text child is what pulls a score/timer/lives value into the layout contract instead of shipping as a loose `add.text`. Group ONLY by **spatial adjacency** (same container), never by semantic symmetry alone (P1/P2 bars on opposite corners are two rows, not one cluster). Full field spec + local-layout semantics: gdd-template §2.0.2 Cluster rows block.
- **Every §2A–2E element gets a §2.0.2 row** (UI + entities + props + decorative); a cluster's children are covered by its `children`/`members` list (text children need no §2E row). This is the "every element corresponds to the layout" contract `gateLayoutAlignment` enforces.
- **`CONFIG.ui` x/y and §7 positions DERIVE from §2.0.2** — compute each from its `box` + `origin` (e.g. a top-left `origin:[0,0]` widget's `x,y = box[0]*W, box[1]*H`; a centered `origin:[0.5,0.5]` widget's `x,y = center*[W,H]`); never type a fresh eyeballed coordinate.

§2.0.2 is the lays-out-like SSOT: scaffold projects it into `.playtest/inputs/layout-contract.json`, implement places every named object from it, and `gateLayoutAlignment` fails unless each rendered element lands within tolerance of its §2.0.2 box. Detection writes to `design/<scene>.detect.json` (keep it next to the mockups, like `crops.json`).

---

## Step 5 — Embed into the GDD + record Ref Crops

1. **§2.0 Visual Coverage Board**: the coverage table from Step 1, plus each size-accurate mockup embedded inline (relative path).
2. **§7 scene layouts**: embed the matching full-screen mockup under each scene so implement-agent matches layout. **§7 is a placement view — it introduces NO new asset.** Every `add.image <key>` you write here must reference a key that already has a §2A–2E row AND a §2.0.2 layout row (§7 keys ⊆ §2.0.2 keys ⊆ §2 keys). If laying out a screen reveals a needed asset (a decorative panel, a portrait, a selection bar), backfill its §2 row + §2.0.2 row FIRST — a §7-only key is an orphan that ships as a blank/Arial fallback. Only a dynamic-value `add.text` may appear without a §2 row.
3. **§2A–2E asset rows**: fill the `Ref Crop` column. For every asset row, point it at the grounded crop for that element:

```markdown
| Entity | Action | View | Frame Size | Key | Ref Crop | Runtime Path | Placeholder | Description |
|--------|--------|------|------------|-----|----------|--------------|-------------|-------------|
| Player | idle | side | 128×128 | player | `design/crops/gameplay/player-soldier.png` | assets/sprites/player/idle-sheet.png | shape(128,128,0x4488ff) | ... |
```

4. **Promote contracted lettering** — every art-directed lettering crop, and
   every `required:true` crop in other profiles, becomes a §2E image row.
5. **No contracted orphan crops** — all crops for `art_directed`, or only
   `required:true` crops otherwise, map to §2 or an authorized waiver.
   A full-mockup `background` crop is reference-only when actors/UI contaminate it: waive it with that reason and keep §2C text-first rather than seeding baked foreground into the runtime map.

---

## Hand-off — crops DRIVE asset generation

For `generated-image` rows, Ref Crop seeds `produce_static_asset` or the
validated sprite pipeline. A generated row with `Ref Crop=—` uses text-only
generation; non-generated rows never enter those tools.

## Quality / cost

- Screen mockups that anchor the look (title, gameplay): `quality: "medium"`. Secondary screens: `quality: "low"`.
- Grounding is one VLM round-trip per mockup (~15-30s); crops are free (local Pillow). Position detection (`detect_layout`, Step 4d) is one more VLM round-trip per mockup — positions only, no crops.
- Crop-seeded production may take longer than text-only generation because the validated static/sprite pipeline preserves concept fidelity and completes its post-processing contract.

## Done criteria

Apply the design-agent's §6 "Completion Contract" and
`gdd-template.md` §§2.0–2E.1 as the authoritative checklist. The checks below
supplement rather than replace that contract. Confirm the concept-owned
artifacts:

- every available mockup is flat-view, shares the authored canvas, and has
  `detect.json`; every successful grounding has crops plus `crops.json`;
- missing references use the exact warning fallback without fake measurements;
- every profile-contracted crop maps to §2 or an authorized waiver;
- Ref Crops seed generated rows, while §2E sizing, bindings, §2.0.2 layout, and
  the `§7 ⊆ §2.0.2 ⊆ §2` key spine remain authored in the GDD.
