# Map & Background Generation Guide

Detailed patterns for generating 2D game maps, backgrounds, parallax layers, props, and tilesets for Phaser games. This guide is adapted from the `phaser-generate2dmap` skill.

## Map Modes

Use the map contract declared in GDD §2 from the mechanics/design axes and
`LEVEL_MODEL`; genre is only a runtime-family pattern hint. The mode determines
the visual/object model, collision approach, and expected deliverables.

### Common modes by runtime family

| Game Genre | Map Mode | Visual Model | Key Traits |
|---------------|----------|-------------|------------|
| `platform` | `side_scroll_mode` | Parallax layers | Separate scenery layers + platform objects + collision |
| `platform` (auto-scroll variant) | `side_scroll_mode` | Parallax layers | Repeating background, procedural obstacle placement |
| `beat_em_up` (side) | `side_scroll_mode` | Parallax layers | Walkable belt polygon + separate props |
| `bullet_hell` / top `beat_em_up` / real-time `roguelike` | `scene_mode` or `tile_mode` | Layered raster or tilemap | Foundation base + separate props + collision zones |
| `tower_defence` | `scene_mode` | Layered raster | Authored model: terrain-only base + runtime geometry from `levels/map-layout.json`; other models use their declared topology source |
| `puzzle` (grid) / grid `roguelike` | `grid_mode` | Tilemap | Grid tiles + cell metadata + object layers |
| `sandbox` / `puzzle` (physics) | `baked_scene_mode` | Baked raster | Single background + physics objects as code |
| `card` / `idle` | N/A | Minimal | Backgrounds are simple, focus on UI assets instead |
| `simulation` | `scene_mode` / `grid_mode` / N/A | per GDD | Depends on whether a movable/managed scene exists |

### Visual pattern examples

When the GDD describes a runtime family rather than a technical map type, these
are visual hints only; they never select `LEVEL_MODEL` or level dispatch:

- Pokemon-like / monster-taming / top-down RPG → `tile_mode`
- Tower defence → `scene_mode`; `LEVEL_MODEL=authored` uses a terrain-only base
  plus `levels/map-layout.json`. Other models use their declared topology authority.
- Survivors-like / arena survival → `scene_mode` with sparse obstacles and spawn zones
- Side-view platform / endless runner (auto-scroll) / Metroidvania → `side_scroll_mode`
- Beat-em-up / brawler → `side_scroll_mode` with walkable belt polygon
- Tactical RPG / strategy grid → `grid_mode`
- Roguelike dungeon / procedural rooms → `room_chunk_mode`
- Visual novel / title screen → `baked_scene_mode`

## Layer Separation Contract

This is the most important rule for map generation. For any playable or editable map, the base/background image must contain only stable non-interactive scenery:

**Base layer may contain:**
- Ground material, water, terrain boundaries
- *Decorative* (non-authoritative) paths/roads — ONLY when movement is free and nothing in the game follows them (e.g. a top-down RPG flavor road)
- Sky, far/mid scenery, distant buildings, atmosphere
- Floor patterns, terrain markings

**Base layer must NOT contain:**
- Trees, rocks, crates, signs, doors, gates, pickups, chests
- Platforms, hazards, traps, turrets, ladders
- Enemies, NPCs, player characters
- **Gameplay-authoritative geometry** — anything logic follows/snaps to/measures.
  Keep it in the model's machine-readable topology authority, not stochastic art.
- Any object that needs collision, interaction, reuse, or independent render order

Why? Baking gameplay objects into the background makes them impossible to collide with, animate, destroy, or reuse. The Phaser game needs them as separate sprites or physics objects.

If a generated background accidentally contains gameplay objects, either regenerate a cleaner version or treat it as a concept reference (not the runtime asset).

**Gameplay-authoritative geometry rule (decouple).** If the game logic reads a position off the art — TD enemies walking a painted lane, units snapping to painted build slots, a core sitting on a painted pad — that geometry must live in a machine-readable SSOT and be rendered by code, never freehand-painted into the base. A stochastic image model cannot reproduce exact coordinates, so a baked gameplay path will never match the path the code walks (it will only *roughly* resemble it). The fix is not "prompt harder" — it is to remove the generator from the authoritative layer. See "Scene Mode (Top-Down / Tower Defence)" → "Tower Defence: map-layout SSOT" for the terrain-only + `map-layout.json` workflow.

## Side-Scroll Mode (Platform / side Beat 'Em Up)

### Near-Layer-First Background Workflow

1. **Define the stage canvas** — use the game's camera/viewport size. Default: `1280x720` from Phaser config, or the GDD's screen dimensions.

2. **Generate the near/background layer first and do not generate separate `sky`, `far-bg`, or `mid-bg` layers by default.** For side-scrolling demos, prioritize making one coherent, high-quality near layer before adding optional extra parallax layers. The required first runtime background is:
   - `assets/maps/[level]-near-bg.png` — the primary non-collidable side-view scenery layer.

   Only add `sky`, `far-bg`, or `mid-bg` if the user explicitly asks for multi-layer parallax after the near layer is accepted.

3. **For wide side-scrolling stages, extend the near layer rightward by outpainting instead of repeating a non-seamless image.** `extend_background` always keeps both the composite input and the full outpainting result beside the output; these review artifacts are mandatory.

   The input must be a distinct local workspace asset and may not collide with
   any path the call owns: the runtime output, its `.asset.json` or `.bbox.json`,
   or the same-basename `-composite.png` / `-outpaint-full.png` review files.
   The tool rejects every such collision before invoking the image provider.

   Required sequence:

   ```
   # 1. Generate and seal the first opaque segment.
   mcp__gca__produce_static_asset(
     prompt="foundation-only side-view level near background, no characters, no UI, no text, stable scenery only",
     output="assets/maps/level1-near-bg.png",
     size="1280x720",
     asset_type="background",
     skip_matting=true,   # opaque full scene; still writes the required .asset.json seal
   )

   # 2. Extend to segment 02 — the tool sends the right-side overlap plus one
   #    full segment-width blank region, restores that overlap pixel-for-pixel,
   #    then crops an exact source-W×H runtime segment and seals its .asset.json.
   mcp__gca__extend_background(
     input="assets/maps/level1-near-bg.png",
     output="assets/maps/level1-near-bg-02.png",
   )
   ```

   The overlap-based canvas keeps common 16:9 segments within the provider's
   3:1 limit. Provider-returned size drift is normalized by aspect-preserving
   cover/crop, never a non-uniform stretch. When using an image-edit/outpainting
   tool manually instead of `mcp__gca__extend_background`, the prompt MUST
   explicitly include: `Ensure the information and visual details on the left
   side are not modified.` Follow the same artifact contract:

   ```text
   assets/maps/level1-near-bg-02-composite.png      # source-right overlap + one full-width blank region
   assets/maps/level1-near-bg-02-outpaint-full.png  # normalized full result with exact source overlap restored
   assets/maps/level1-near-bg-02.png                # exact source-W×H runtime segment 02
   assets/maps/level1-near-bg-02.asset.json         # producer/hash/dimensions/provenance/lineage/review paths
   ```

   Then repeat from the cropped segment:

   ```text
   assets/maps/level1-near-bg-03-composite.png      # segment-02 right overlap + one full-width blank region
   assets/maps/level1-near-bg-03-outpaint-full.png  # normalized full result with exact source overlap restored
   assets/maps/level1-near-bg-03.png                # exact source-W×H runtime segment 03
   assets/maps/level1-near-bg-03.asset.json         # lineage now includes prior extension ancestry
   ```

   Hard gate: do not delete the `*-composite.png` or `*-outpaint-full.png`
   review artifacts. `extend_background` saves them before publishing the
   runtime segment, programmatically restores the original overlap into the
   full result, and transactionally publishes them with the runtime segment.
   Their stable paths and SHA-256 hashes, plus the source hash/lineage, are
   sealed in the same-basename `.asset.json`; do not rely on the image-edit
   prompt alone.
   `finalize_asset_manifest` accepts this producer for generated-image
   `background` rows only after reopening the runtime PNG, matching its real
   dimensions to the sidecar, and revalidating source/lineage/review hashes.
   Place `level1-near-bg.png`,
   `level1-near-bg-02.png`, `level1-near-bg-03.png`, etc. side by side in
   Phaser. Only use texture repeat or `TileSprite` when the image was explicitly
   generated as a seamless tile.

4. **Generate a stage reference** (optional but recommended) — use edit mode with the accepted near background as reference to visualize where platforms, hazards, and pickups will go. This is a planning artifact, not a runtime asset.

5. **Generate platforms and props as separate assets** — transparent sprites for ground chunks, floating platforms, hazards, pickups, doors, etc.

6. **Define placement and collision** in code or JSON metadata.

### Phaser Near-Layer Setup

```javascript
// preload():
this.load.image('near-01', 'assets/maps/level1-near-bg.png');
this.load.image('near-02', 'assets/maps/level1-near-bg-02.png');
this.load.image('near-03', 'assets/maps/level1-near-bg-03.png');

// create(): place accepted segments side by side; do not repeat a non-seamless texture.
const segmentWidth = CONFIG.screen.width;
['near-01', 'near-02', 'near-03'].forEach((key, index) => {
  this.add.image(index * segmentWidth + segmentWidth / 2, CONFIG.screen.centerY, key)
    .setDisplaySize(segmentWidth, CONFIG.screen.height)
    .setScrollFactor(0.6);
});

// Platforms, props, player are scroll factor 1 (default)
```

Optional extra parallax layers may be added later only after the near layer is accepted.

## Scene Mode (Top-Down / Tower Defence)

### Layered Map Workflow

1. Generate `assets/maps/[name]-base.png`. For top-down RPG maps, decorative
   roads are fine. For `tower_defence + authored`, keep it terrain-only because
   runtime geometry comes from `map-layout.json`. Other TD models follow their
   declared topology/rendering strategy.

2. **Generate a dressed reference** (optional) — use edit mode with the base as reference to visualize prop placement. Show proposed objects as natural game-world items. This is a planning reference, not runtime.

3. **Generate every prop independently** with `produce_static_asset`. Compact
   props, large props, platforms, and tiles each get their own call and their
   own alpha/matting result. Do not generate a multi-object grid and split it.

4. **Define placement metadata** — either in code or as `design/[name]-props.json`
   for asset preview/tooling. Asset MCP tools only read/write `assets/**` and
   `design/**`; if the final game needs runtime `data/**`, implement-agent may
   copy/translate the accepted design metadata into its own runtime-authority
   location later.
   ```json
   [
     {"key": "rock1", "x": 200, "y": 150, "depth": 1},
     {"key": "tree1", "x": 400, "y": 300, "depth": 2}
   ]
   ```

5. **Load in Phaser** (use post-processed transparent props, NOT raw images):
   ```javascript
   // Load base + props in preload()
   this.load.image('map-base', 'assets/maps/forest-base.png');
   this.load.image('rock1', 'assets/props/rock1.png');
   this.load.image('tree1', 'assets/props/tree1.png');

   // Place in create()
   this.add.image(0, 0, 'map-base').setOrigin(0, 0);
   // Props from metadata...
   ```

### Tower Defence: `map-layout.json` SSOT + mask soft-hint

For `tower_defence + LEVEL_MODEL=authored`, path/core/spawn/build-slot geometry
is owned by `levels/map-layout.json` and rendered at runtime. Procedural/board
models use their declared topology source instead.

To keep the painted terrain visually cohesive with where the road will sit, you MAY seed generation with a **soft mask hint** (NOT a hard control — the gpt-image-2 edit endpoint honors a reference only loosely, and there is no ControlNet in the stack):

```jsonc
// 1. Render a path mask from the layout SSOT (road polylines + node markers).
mcp__gca__render_layout_mask({
  layout: "levels/map-layout.json",
  output: "design/map-layout-mask.png"
})

// 2. Use it as a soft reference so the terrain hints at the road corridor
//    (worn ground / energy seams under the lanes) WITHOUT baking a literal road.
mcp__gca__edit_image({
  input: "design/map-layout-mask.png",
  prompt: "Top-down sci-fi terrain. Subtle worn ground / energy seams ALONG the faint guide lines, but do NOT draw literal roads, glowing rails, nodes, or UI — terrain texture only, no objects, no path network.",
  output: "assets/maps/map-base.png",
  size: "<game canvas W>x<game canvas H>",
  asset_type: "map"   // REQUIRED: keeps the full opaque terrain. Without it,
                      // edit_image now defaults to a removable #FF00FF fill
                      // (correct for lettering/props, WRONG for a map base).
})
```

The mask is decoration guidance only. **Correctness is guaranteed by the code rendering the road/props from the layout, and by the `gateMapLayoutPresent` / `gateNoBakedPath` gates — not by how faithfully the model followed the mask.** If you skip the mask entirely, a plain terrain `generate_image` is equally acceptable; the gameplay geometry is unaffected either way.

## Prop Classification

Before generating any prop, classify it to choose the right generation strategy:

| Classification | Description | Strategy |
|---------------|-------------|----------|
| `compact_prop` | Small/medium, roughly square, decorative | One-by-one via `produce_static_asset` |
| `wide_or_long` | Aspect ratio > 1.6:1 (platforms, bridges, fences) | One-by-one with a wide aspect |
| `tall_or_large` | Tall dominant objects (large trees, gates, towers) | One-by-one |
| `collision_bearing` | Must align with collision shapes (platforms, doors) | One-by-one |
| `tileset_piece` | Should repeat seamlessly (floor tiles, wall tiles) | One tile per asset; make seamless when needed |

## Visual Reference Handoff

When generating a dressed reference or stage reference from a base/background:

1. Save the base image first
2. Use `mcp__gca__edit_image(input="base.png", prompt="...", output="...", size="<game W>x<game H>", asset_type="map")` to generate the reference — pass the game aspect explicitly and use `asset_type="map"` (or `background`/`parallax`) so the edit keeps its OPAQUE full scene.
3. In the prompt, explicitly describe what to preserve from the base: camera framing, terrain boundaries, road shapes, horizon position
4. The reference should show objects as natural game-world items — not circles, arrows, labels, or annotations
5. Keep references sparse: at most 9 distinct object types. Repeated instances of the same object count as one type

## Tileset Mode (Puzzle / RPG)

1. **Generate each tile independently** with `produce_static_asset`; do not ask the image model to compose a multi-tile grid.
2. **Define tile metadata** — walkable/buildable flags, terrain types, collision
3. **Build tile layers** using the genre's existing authored level/board schema;
   each cell references an individual tile key.
4. **Use the loader already declared by that schema.** DSL genres may route
   through their generated LevelLoader/TerrainAssembler contract; puzzle games
   use their board renderer. This guide does not invent a universal `tiles`
   field or `terrainAssembler.build()` API. If the chosen runtime specifically
   requires an atlas, create it with a deterministic packer in code—not with
   image generation—and record that packer contract before asset production.

## Baked Scene Mode (Simple Backgrounds)

For non-playable backgrounds, title screens, or simple physics sandbox backgrounds:

1. Generate a single full-scene image
2. Load as a background image in Phaser
3. Game objects are created in code, not from the background image

This is the simplest mode — use it only when the background has no gameplay objects.

## Expected Deliverables by Mode

### Side-Scroll Mode
```
assets/
├── map/
│   ├── level1-near-bg.png
│   ├── level1-near-bg-02-composite.png      (review artifact, keep)
│   ├── level1-near-bg-02-outpaint-full.png  (review artifact, source overlap restored pixel-identical)
│   ├── level1-near-bg-02.png
│   ├── level1-near-bg-02.asset.json         (generated-static seal + source lineage/review paths)
│   ├── level1-near-bg-03-composite.png      (review artifact, keep)
│   ├── level1-near-bg-03-outpaint-full.png  (review artifact, source overlap restored pixel-identical)
│   ├── level1-near-bg-03.png
│   ├── level1-near-bg-03.asset.json         (generated-static seal + source lineage/review paths)
│   ├── level1-stage-reference.png           (planning only, not runtime)
│   └── *.prompt.txt
├── props/
│   ├── platform-left.png
│   ├── platform-mid.png
│   ├── platform-right.png
│   ├── spike.png
│   └── coin.png
design/
└── level1-objects.json   (asset preview placement, collision, spawn metadata)
```

Do not generate `level1-sky.png`, `level1-far-bg.png`, or `level1-mid-bg.png` by default for side-scroll demos. Add those layers only if the user explicitly asks for extra parallax after the near layer has been accepted.

### Scene Mode (Top-Down)
```
assets/
├── map/
│   ├── forest-base.png
│   ├── forest-dressed-reference.png  (planning only)
│   └── *.prompt.txt
├── props/
│   ├── rock.png                     ← Individually generated transparent prop
│   ├── tree.png
│   └── bush.png
design/
├── forest-props.json     (asset preview placement metadata)
└── forest-collision.json (asset preview collision zones)
```

### Baked Scene Mode
```
assets/
├── map/
│   ├── arena.png
│   └── arena.prompt.txt
```

## Post-Processing Map Props

Every prop must be transparent before use. Generate it through
`produce_static_asset`, which owns safe-color staging, configured matting
(`rmbg2` preferred or `color_key` fallback), trimming, and optional
scaling/bbox. Raw props with visible colored backgrounds must never be loaded by
the game.

### Compact Prop Workflow

```python
# Repeat one atomic call per prop; independent calls may run concurrently.
mcp__gca__produce_static_asset(
    prompt="clean HD 2D game asset, mossy rock, top-down RPG view, single centered object, no text",
    output="assets/props/mossy-rock.png",
    asset_type="prop",
    collidable=True,
)

# Compose a preview to check placement.
mcp__gca__postprocess(
    command="compose-preview",
    base="assets/maps/forest-base.png",
    placements="design/forest-props.json",
    output="assets/maps/forest-preview.png",
)
```

### Single Large Prop Workflow

```python
# Atomic generation → configured matting → trim → optional collision bbox
mcp__gca__produce_static_asset(
    prompt="clean HD 2D game asset, large oak tree with spreading canopy, top-down RPG view, single centered tree, no text",
    output="assets/props/large-oak.png",
    asset_type="prop",
    collidable=True,
)
```

### What NOT to Matte or Split

- **Map backgrounds** (forest-base.png, sky.png, far-bg.png) — generate through
  `produce_static_asset({asset_type:"background", skip_matting:true})`; they are
  opaque full scenes but still require the metadata seal
- **Battle backgrounds** — same, full-scene baked images
- **Parallax layers** — scenery images that fill the viewport

Use `produce_static_asset` for normal static props/UI so matting and alpha
verification cannot be skipped. Only legacy keyed intermediates need the
explicit `rmbg2_matting` or `color_key_matting` → `trim_transparent` sequence.
