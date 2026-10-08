# Post-Processing Guide

Complete reference for converting raw AI-generated images and Seedance i2v videos into game-ready transparent assets. Sprite actions use `mcp__gca__rmbg2_video_matting` to produce transparent frame/GIF preview intermediates, then `mcp__gca__pack_spritesheet` to produce the Phaser runtime spritesheet PNG + JSON. Static assets (props, tiles, UI) use `mcp__gca__produce_static_asset`, which welds generation/editing → configured `assets.matting.backend` (`auto`, `rmbg2`, or `color_key`) → `trim_transparent` → optional scale/bbox into one call. `auto` prefers RMBG and remattes the complete still or video frame sequence with color-key on network, missing-route HTTP 404, or HTTP 5xx outages. `mcp__gca__postprocess` covers only the CPU-only `compose-preview` and `make-seamless` helpers.

## Why Post-Processing is Mandatory

Standalone game art should be generated with transparent backgrounds whenever the generator supports it. If an image generator cannot reliably produce transparency for a grid/sheet, use a temporary keyed colored background — typically magenta `#FF00FF` — and post-process it before use. Keyed raw images cannot be used directly in a game because:

1. **The background is visible** — a pink rectangle appears behind your sprite/prop in-game
2. **Antialiased edges** contain semi-transparent pink fringe pixels that create visible halos
3. **Seedance videos** need their solid staging background removed into transparent frames/GIF previews, then packed into Phaser spritesheet PNG + JSON runtime assets
4. **Props** need to be produced individually with proper alpha transparency

The `mcp__gca__rmbg2_video_matting` tool handles video-to-transparent-frame/GIF-preview background removal with the configured backend; `auto` uses RMBG first and, on a network, missing-route HTTP 404, or HTTP 5xx failure, discards partial results and color-keys every frame. `mcp__gca__pack_spritesheet` is still the required final runtime packing step for animated sprites. Legacy grid splitting has been removed. Individual assets are generated via `produce_static_asset` or the i2v pipeline rather than generated as a grid and split.

## Prerequisites

Use the `gca` MCP tools; agents never install dependencies or invoke the
bundled runners directly. The MCP environment provisioned by `uv sync`
contains Pillow and NumPy.

## Subcommand Reference

### Single-Image Background Removal

For newly generated static props, tiles, and UI, use
`mcp__gca__produce_static_asset`. It atomically generates/edits, runs
the configured matting backend, trims the transparent margin, and optionally
scales or measures the asset. The default `auto` policy prefers RMBG-2.0 and
uses `color_key` only for fallback-eligible service outages. The color-key path fails
closed when the selected key removes
no pixels or erases the entire foreground, so it cannot seal an opaque or empty
matte. Opaque background/map/parallax calls use `skip_matting:true`, but
still finish through the same atomic `.asset.json` seal: final path, artifact
hash, dimensions, producer, provenance, and source contract. After all workers
join, `finalize_asset_manifest` reopens every static artifact, rejects unreadable
files or sidecar/file dimension drift, and verifies those fields (plus
collidable bbox sidecars) before its single atomic manifest merge. Sidecar
width/height values are evidence to compare, never measurements to trust.
Color-key sidecars additionally seal `mattingKeyColor` in provenance and the
source contract so the deterministic matte remains reproducible.

When `ref_image` is present, it must be an existing canonical local path under
the active workspace's `assets/**` or `design/**`. `produce_static_asset` does
not accept a URL or repository-global reference for this route. It seals the
workspace-relative `referenceArtifact` and `referenceArtifactSha256` in both
provenance and `source_contract`; finalization requires that reference to still
exist with the same hash.

`produce_static_asset.asset_type` is closed to canonical static categories:
`background`, `map`, `parallax`, `prop`, `item`, `tile`, `tileset`, `platform`,
`obstacle`, `ui`, `fx`, `effect`, and `impact`. UI-like names are not separate
producer types: HUDs, icons, buttons, panels, badges, gauges/meters,
dashboards/toolbars/counters/widgets, lettering, logos, and wordmarks all use
`asset_type:"ui"` (including art-word, frame, and overlay rows). During
finalization those same manifest UI categories map to
`source_contract.assetType:"ui"`; every other generated static row must match
its manifest category exactly.

UI sizing is profile-routed and mutually exclusive:

- `art_directed`: exactly one `target_height` or `target_width`;
- `pixel_art`: exactly `base_size:[width,height]`; the trimmed art is
  aspect-preserving contain-fit and transparent-padded to that exact box using
  nearest-neighbor only;
- `responsive` / `minimal`: exactly one `max_box:[width,height]` for every UI
  image. Decorative frames, overlays, logos, and lettering have no target-axis
  exemption.

The producer records `source_contract.qualityProfile` and exactly one
`source_contract.sizingContract`. That object carries the profile's one authored
field (`targetWidth`, `targetHeight`, `maxBox`, or `baseSize`), measured
`finalDimensions`, `aspectPreserved:true`, and the canonical `resampler`
(`lanczos` or `nearest-neighbor`). New sidecars must not duplicate those values
in legacy peer fields. Pixel-art UI never passes through LANCZOS.

The reducer requires manifest `qualityProfile` to match the generated UI
sidecar. It rejects extra/conflicting manifest sizing modes and value drift:
pixel texture dimensions equal `baseSize`, the art-directed target axis equals
the PNG, and responsive/minimal dimensions fit the manifest's one matching
`maxBox` or `logicalBox`. Pixel manifests also retain a non-empty positive
`integerScales` list; that runtime policy is not duplicated into the producer
sidecar. The reducer validates but does not rewrite those authored sizing fields;
only measured width/height and other producer-owned measurements merge.

Every mutating path requires an active workspace and must resolve under
`assets/**` or `design/**`; tool-owned slot evidence alone may write under
`.playtest/verify_middle/slot/**`. Absolute paths and symlinks do not bypass
containment, and asset tools never write `src/**`, `.playtest/state.json`, or
`.playtest/verify/**`.

For an existing keyed intermediate, run the same stages explicitly:

```python
mcp__gca__rmbg2_matting(
    input="assets/props/raw/large-tree.png",
    output="assets/props/large-tree.png",
    bg_hex="#FF00FF",
    aug_preset="off",
)
mcp__gca__trim_transparent(
    image="assets/props/large-tree.png",
    padding=2,
)
```

The `remove-bg` postprocess subcommand does not exist. The `postprocess` tool
is reserved for preview composition and seam fixing. For color-key background
removal without an RMBG-2.0 deployment, use `color_key_matting` (below).
Animated sprites use `rmbg2_video_matting` and must keep equal-sized frames
rather than trimming each frame independently.

### color_key_matting — Pure-Computation Safe-Color Fallback

Use only when no RMBG-2.0 service is available (`assets.matting.backend:
color_key`, or an explicit call). This is a deterministic CPU-only color-key
matte for `#FF00FF` or `#00F000` staging backgrounds — no model, no API. RMBG-2.0
(`rmbg2_matting`) remains the default and produces cleaner soft-alpha edges;
prefer it whenever a local or remote RMBG-2.0 endpoint is deployed.

```python
mcp__gca__color_key_matting(
    input="assets/props/raw/large-tree.png",
    output="assets/props/large-tree.png",
    key_color="#FF00FF",
)
```

It clears pixels near the selected key (global `threshold`) and flood-fills
background-connected key pixels from the image borders (`edge_threshold`).
Choose the reserved safe color absent from the foreground. The tool rejects
invalid threshold ranges, a matte that removed no key pixels, and a matte that
erased the entire foreground.

### compose-preview — Layered Map Preview

Use to verify prop placement by compositing individually generated props over a base map.

```python
mcp__gca__postprocess(
    command="compose-preview",
    base="assets/maps/forest-base.png",
    placements="design/forest-props.json",
    output="assets/maps/forest-preview.png",
    report="design/preview-report.json",
)
```

**Placement JSON format:**
```json
[
  {
    "id": "rock1",
    "image": "assets/props/mossy-rock/prop.png",
    "x": 200, "y": 400,
    "w": 64, "h": 48,
    "anchor": "center-bottom",
    "sortY": 400
  }
]
```

Supported anchors: `top-left`, `center`, `bottom-left`, `center-bottom` (default).

Props are sorted by `sortY` for correct overlap ordering.

## Troubleshooting

### Pink/magenta fringe visible around sprites

The staging background was not cleanly separable. Regenerate on a permitted
solid safe color and let RMBG-2.0 handle the matte. The `color_key_matting`
fallback leaves harder edges than RMBG-2.0, so prefer RMBG-2.0 whenever a
deployment is available.

### Subject gets partially erased

The safe color conflicts with the subject. Regenerate using the other permitted
safe color from `sprite-guide.md` §2 and pass that color as `key_color` for an
explicit color-key call rather than tuning thresholds around a bad staging image.

## Asset Type Quick Reference

| Asset Type | MCP Tool | Key Args |
|-----------|------------|-----------|
| Character idle | `rmbg2_video_matting` | Use configured safe-color/matting values |
| Character walk/run | `rmbg2_video_matting` | Use Seedance i2v first frame, then AI matte |
| Particle / glow effects | **none — runtime FX** | Runtime-only FX contract: these are `ParticleEmitter` recipes (fx-guide), NOT generated/matted files. Do not route through `rmbg2_video_matting`. |
| Projectile *body* loop | `rmbg2_video_matting` | Generate via `animation_profile="projectile"` (centered, no ground contact); its trail/impact is a separate runtime particle recipe, not baked in |
| Impact / explosion | **none — runtime FX** | Runtime-only FX contract: `ParticleEmitter` recipe (fx-guide), NOT a generated/matted file |
| Existing keyed static image | `rmbg2_matting` → `trim_transparent` | Optional `bg_hex` + `aug_preset` for color rescue |
| Keyed image, no RMBG-2.0 deployment | `color_key_matting` | `#FF00FF` or `#00F000`; set matching `key_color`; harder edges than RMBG-2.0 |
| Static prop / tile | `produce_static_asset` | `asset_type`, optional `collidable` and target size |
| UI button / widget (`art_directed`) | `produce_static_asset` | `asset_type="ui"`, exactly one `target_height`/`target_width`, optional crop/anchors/slots |
| UI button / widget (`pixel_art`) | `produce_static_asset` | `asset_type="ui"`, exact two-int `base_size`; nearest-neighbor contain/pad |
| UI button / widget (`responsive`/`minimal`) | `produce_static_asset` | `asset_type="ui"`, required `max_box`, optional crop/anchors/slots |
| Map background | `produce_static_asset({asset_type:"background", skip_matting:true})` | Opaque full scene; no matte, but metadata seal required |
| Parallax layer | `produce_static_asset({asset_type:"parallax", skip_matting:true})` | Opaque scenery; no matte, but metadata seal required |
