# Prompt Writing Rules

Complete reference for writing effective AI image generation prompts for game assets. Write every prompt manually — do not use scripts to generate creative prompts.

## Global Rules

These constraints apply to generated game assets:

- **Flat 2D VIEW — not flat shading.** What's locked is the *view/projection*, so the flat Phaser runtime can place the sprite: each asset is a 2D game sprite seen through a flat camera (the `side` / `topdown` / `3/4` View Angles below — a stylized viewing angle on a flat sprite). NO isometric / axonometric scene framing, NO 3D perspective / vanishing points, NO object drawn as an extruded 3D body / perspective box with receding side faces, NO full-CGI / clay / sculpted *scene*, NO cast-from-height drop shadows. What's NOT locked is the *finish*: render each sprite as a polished illustration in the GDD's chosen art style — soft/gradient shading, volume, highlights, rim light, and rich materials are encouraged so assets look refined (go truly flat/cel only when the chosen style is explicitly flat/vector). **Depth may be painted via shading, never projected via perspective.** Tiled/stitched assets are the exception that must additionally stay orthographic & seam-safe (see the Platform/Tileset rules).
- All generated non-background image assets use a removable safe background and
  matting. One asset per generation call. `art_directed` effects use runtime
  particle recipes; other profiles may generate only a GDD-declared flipbook.
  Projectile bodies remain separate from trails/impacts under every profile.
- Sprite action first frames for Seedance i2v should use a solid `#FF00FF` background with `safe_bg_color` param, because the video post-process step mattes that background out via RMBG2.
- Full-scene map backgrounds and battle backgrounds are the exception; they are normally opaque and fill the screen.
- No text, labels, UI, or speech bubbles anywhere
- Do not request grids or sprite sheets — generate each tile/prop individually.
- No borders or frames around first-frame images.
- Preserve the same asset identity from reference to first frame and from first frame to i2v prompt.
- Keep the intended runtime scale and composition stable.
- Raw art must come from the image generation API, not from Canvas/SVG/PIL drawing

## Art Style Selection

Choose the style BEFORE writing the prompt. The style must be consistent across all assets in a game.

| Style | When to Use | Prompt Keywords |
|-------|-------------|-----------------|
| `pixel_art` | Classic 2D game sprites, general default | "pixel art", "2D game sprite", "crisp dark outlines" |
| `clean_hd` | Polished games, map props, HD style | "clean hand-painted HD 2D game asset", "crisp silhouettes", "smooth surfaces", "no chunky pixels" |
| `pixel_inspired` | Modern pixel-adjacent without retro feel | "clean modern pixel-art-inspired", "no heavy dithering" |
| `retro_pixel` | Only when explicitly requested | "16-bit pixel art", "retro JRPG style" |

Do not write `16-bit`, `retro JRPG`, or `chunky pixel-art` unless the user/GDD asks for that look. For clean HD props, explicitly say "Do not make pixel art" in the prompt.

## Containment Rules

For action first frames, say these explicitly:

- The entire subject must fit fully inside the canvas
- No body part, weapon, tail, wing tip, or projectile body may be cropped;
  detached FX are not generated
- Grounded character/creature profiles: feet or the lowest body point touch the
  bottom edge, with padding above and on both sides and no empty strip below
- Projectile profiles: center the body with comfortable padding on all four sides
- Use a plain solid background for video keying
- Keep the intended runtime scale and the profile-specific anchor

Do not request generated grids, sprite sheets, prop packs, tile packs, or icon
packs. Generate every static asset independently with `produce_static_asset`.
Detached FX are never part of generated body art; they are runtime
ParticleEmitter recipes.

## View Angle Rules

| View | Use For |
|------|---------|
| `topdown` / `3/4` | Overworld actors, top-down game player/NPC first frames |
| `side` | Side-view platform sprites, projectiles, impacts |
| `3/4` | Creature battle sprites, bosses, showcase idles |

## Prompt Templates by Asset Type

### Front-View Character Reference

Use this before generating action first frames for identity-sensitive sprites.

```
[art_style] single [asset_type] front-view reference on a solid pure #FF00FF (magenta) background filling the entire canvas edge-to-edge; use #00F000 (green) instead if the subject contains magenta/pink.
[Character description: appearance, costume, colors, distinguishing features, materials].
Character faces the camera directly, full frontal view only.
NOT back view, NOT side view, NOT 3/4 view.
Full body visible, standing neutral pose, centered in canvas.
Generous padding on all sides against the solid background.
Clear readable silhouette, stable palette, no alternate versions.
No text, no labels, no UI. Solid safe-color background only, edge-to-edge.
```

### Reference-Based Action First Frame

Use this with `mcp__gca__edit_image(input="assets/sprites/references/[asset-key]-front.png", prompt=..., output="...")`.

```
[art_style] first frame for [action] animation.
Preserve the reference character identity, silhouette, palette, costume details, face/eye features, accessories, materials, and style.
[View direction] view.
Single still image only, not an animation sheet.
Character is centered at intended runtime scale on a solid pure #FF00FF (magenta) background filling the entire canvas edge-to-edge; use #00F000 (green) instead if the subject contains magenta/pink.
Full body visible; feet/lower body touch the bottom canvas edge, with generous
padding above and on both sides and no empty strip below the feet.
No motion blur, no duplicate poses, no grid, no sprite sheet, no borders.
No text, no labels, no UI.
```

### Character / Player First Frame

Use this direct text-to-image template only when no reference is needed. For heroes, enemies, NPCs, and recurring characters, prefer the front-view reference plus reference-based first-frame template above.

```
[art_style] first frame for [action] animation.
[Character description: appearance, costume, colors, distinguishing features].
[View direction] view.
Single still image only, not an animation sheet.
Character is centered at intended runtime scale on a solid pure #FF00FF (magenta) background filling the entire canvas edge-to-edge; use #00F000 (green) instead if the subject contains magenta/pink.
Full body visible; feet/lower body touch the bottom canvas edge, with generous
padding above and on both sides.
No motion blur, no duplicate poses, no grid, no sprite sheet.
No text, no labels, no UI.
```

### Directional Walk First Frame (Top-Down)

```
[art_style] top-down RPG character first frame for walking [direction].
3/4 top-down view, [character description].
Facing [down/left/right/up], neutral walk-start pose.
Single still image only for i2v.
Character centered at intended runtime scale on a solid pure #FF00FF (magenta) background filling the entire canvas edge-to-edge; use #00F000 (green) instead if the subject contains magenta/pink.
The lowest body point touches the bottom canvas edge; keep margin above and on both sides.
No grid, no sprite sheet, no text, no labels.
```

### Creature / Boss Action First Frame

```
[art_style] first frame for [idle/roar/walk/attack] animation.
[3/4 or side] view, [creature description].
Single still image only for i2v.
Creature centered at intended runtime scale on a solid pure #FF00FF (magenta) background filling the entire canvas edge-to-edge; use #00F000 (green) instead if the subject contains magenta/pink.
Clear readable silhouette, full body visible; lowest body point touches the
bottom canvas edge with margin above and on both sides.
No grid, no sprite sheet, no text, no labels.
```

### Projectile First Frame

This is for the projectile body only. Trail/impact/explosion are separate and
follow the profile/GDD effect strategy.

```
[art_style] first frame for projectile loop animation.
[Side/top-down] view, [projectile body description — no trail, no impact flare, no explosion].
Single projectile body only, centered on a solid pure #FF00FF (magenta) background filling the entire canvas edge-to-edge; use #00F000 (green) instead if the subject contains magenta/pink.
Keep clear safe-color margin on all four edges; the projectile never touches the bottom edge.
No duplicate frames, no grid, no sprite sheet.
No text, no labels.
```

### Seedance i2v Prompt

Use this prompt with `mcp__gca__generate_seedance_video(first_frame=..., prompt=...)`.

```
Keep the first-frame composition and character size unchanged. The [character/object] in the center of the [plain-background description] performs a looping [action] animation.
Motion is natural; do not scale the character up, do not crop, do not change the canvas composition.
Preserve the original character appearance, colors, silhouette, and style.
No attacking, no violent content, unless the action explicitly requires it.
```

### Map Background (Side-View)

```
Game background art for a [style] [genre] game.
[Description of the scene: environment, mood, time of day, key landmarks].
Side-view perspective, edge-to-edge, high detail.
[Dimensions: e.g., 800x600 or 1536x864].

IMPORTANT: This is scenery/atmosphere ONLY.
NO platforms, NO walkable floors, NO gameplay objects.
NO characters, NO enemies, NO items, NO UI, NO text.
Distant scenery, sky, atmosphere, and terrain silhouettes only.
Keep the foreground lane visually open and neutral.
```

For rightward background continuation via `mcp__gca__extend_background`, the default prompt is already in the tool; pass a custom `prompt` only when you need to override. As a reference, the default pattern is:

```
Fill only the blank right half as a seamless horizontal continuation of the game background on the left.
Ensure the information and visual details on the left side are not modified.
Preserve the left half exactly. Do not repaint, crop, resize, blur, shift, or change any pixel on the left half.
Continue the same perspective, major guide lines, lighting, art style, color palette, and horizon alignment.
No characters, no enemies, no gameplay objects, no UI, no text, no labels, no borders.
The result should look like one continuous side-scrolling game background.
```

### Map Background (Top-Down)

```
Top-down 2D RPG game map, [art_style].
[Description: terrain, paths, water, ground materials].
Bird's-eye view, [dimensions].

FOUNDATION ONLY — ground terrain, paths, roads, water, terrain boundaries.
NO trees, NO rocks, NO buildings, NO crates, NO signs, NO objects.
NO characters, NO enemies, NO NPCs, NO UI, NO text.
Clean readable terrain shapes, consistent lighting.
```

### Single Prop (One-by-One)

```
[art_style] 2D game asset sprite on a plain removable safe-color background.
[Description of the object: shape, colors, style].
[Perspective] view. Single object only, centered.
Generous plain-background padding on all sides.
ONE object only, no text, no labels, no additional elements.
```

Call `produce_static_asset` once per prop; it supplies the key background,
mattes, trims, and optionally scales/measures the result.

### Platform Tile (single tile per call)

```
[art_style] 2D game platform tile.
Single [left end cap / repeatable middle / right end cap] tile.
Orthographic flat side-on view. NO perspective, NO foreshortening, NO 3D
depth, NO near-large/far-small. The top surface is a flat horizontal band.
[description of platform material/style].
Single tile centered on a plain solid pure magenta (#FF00FF) background.
No text, no labels, no grid, no scene.
```

**Why orthographic (MUST for any tiled/stitched asset):** perspective is not
translation-invariant, but tiling is — a tile drawn with depth (near-large,
far-small, visible thickness) cannot repeat or butt against its neighbour
without a visible break. Draw stitched assets flat/side-on so the connecting
axis is uniform.

**Shared connecting-face contract (all 3 pieces):** the left cap, middle, and
right cap MUST share the SAME content height and the SAME top-surface band
height, and the cap **inner** faces must match the middle's vertical cross-
section (same height, same edge profile) so they butt seamlessly. State this in
each piece's prompt, e.g. "the right edge of this left-cap is a flat vertical
face NN px tall matching the middle tile's left edge". Mismatched piece heights
are baked into the art and cannot be fixed in code.

Call `produce_static_asset` separately for each tile variant (left cap, middle,
right cap, etc.). Do NOT generate a multi-tile grid image.

### Tileset Tile (single tile per call)

```
[art_style] single seamless tileset tile on solid pure magenta (#FF00FF) background.
THEME: [terrain/environment description].
Tile type: [e.g., grass center / grass-to-dirt left edge / path corner].
Orthographic flat 2D top-down or side-on view. NO perspective, NO 3D depth.
Consistent flat lighting (no directional shadow that breaks on repeat).
Single tile centered with generous padding. No text, no labels, no grid.
FORBIDDEN: multiple tiles, grid layouts, borders, 3D perspective, foreshortening.
```

Call `produce_static_asset` separately for each tile type. Do NOT generate a
multi-tile grid and split it.

### Pipe / Tube / Beam segment (stitched, single segment per call)

Cylindrical or beam-like stitched assets (pipes, tubes, logs, girders) have the
same constraint as platforms, plus one: do NOT draw the open **end hole** on the
repeatable body — the hole belongs only to the end cap.

```
[art_style] 2D game [pipe/tube/beam] middle segment.
Orthographic flat side-on view. NO perspective, NO foreshortening, NO visible
open end / hole / bore. Uniform constant cross-section along the horizontal
axis so it tiles seamlessly; left and right edges are identical flat faces.
[material/style]. Solid pure magenta (#FF00FF) background. No text, no scene.
```

The end caps (the openings / mouths) are generated as separate left/right cap
pieces, matching the body's cross-section height.

> **Seamless middles**: a repeatable middle tile (`*-mid`, floor/wall `tileset_piece`) is never truly tileable straight from the generator — butting two copies shows a hard seam. After matting, run `mcp__gca__postprocess(command="make-seamless", input=…, output=…)` on the middle piece (CPU offset-blend, no API). End caps are not run through this. See `references/implement/composite-tiling.md`.

### UI Button

```
[art_style] 2D game UI button on a plain removable safe-color background.
[Shape description: rounded rectangle, pill shape, ornate frame, etc.].
[Color scheme and visual style].
[Text on button if any, with font style].
Single button centered in the canvas with generous plain-background padding.
Clean crisp edges, high contrast, easily readable at game resolution.
ONE button only. No other UI elements.
No extra panel, scene, or unrelated elements.
```

### UI Health/Status Bar

```
[art_style] 2D game health bar UI element on a plain removable safe-color background.
[Bar style: simple rectangle, ornate frame, heart-based, etc.].
[Colors: fill color, empty color, frame color].
Horizontal bar, [approximate width]x[approximate height] pixels.
Centered in canvas with generous plain-background padding.
IMPORTANT: Just the bar frame/container — fill level is handled in code.
ONE element only. No text, no numbers, no other UI.
```

### UI Icon (one icon per call)

```
[art_style] 2D game icon on a plain removable safe-color background.
[Single icon description and theme].
One icon centered with generous plain-background padding.
Match the shared icon style, scale, and line weight.
Clean crisp edges, high contrast, easily readable at small sizes.
No text, labels, grid, or additional icons.
```

## Layout Guide Usage

Layout guides belong to concept-screen layout detection, not asset generation.
Do not feed a grid guide to the image model for props, tiles, icons, or sprite
frames. Generate every asset independently and use the authored layout contract
to place it at runtime.

## Content Safety & Sensitive Word Avoidance

AI image/video generation services have content moderation filters. When a prompt triggers moderation (returns `OutputVideoSensitiveContentDetected`, `CONTENT_FILTERED`, HTTP 451, or similar), **do NOT retry with the same prompt**. Modify the prompt to avoid sensitive content, then retry.

### Known Sensitive Words (DO NOT USE)

These words/phrases are known to trigger content filters on Seedance and image generation APIs. Use the safe alternatives instead:

| ❌ Sensitive | ✅ Safe Alternative |
|---|---|
| soldier, military, army, troops | runner, agent, operative, character |
| weapon, gun, rifle, pistol, firearm | blaster, energy tool, device, gadget |
| shoot, fire (weapon), kill, attack | blast, activate, trigger, discharge |
| war, battle, combat, fight | challenge, encounter, clash, contest |
| bomb, grenade, explosive, missile | energy orb, pulse charge, projectile |
| blood, gore, wound, injury | sparks, damage flash, impact mark |
| death, die, corpse | defeat, deactivate, collapse, fall |
| knife, sword, blade (offensive) | energy blade, beam, light edge |
| tank (military vehicle) | heavy mech, spider walker, ground unit |
| helicopter, warplane, fighter jet | drone, hovercraft, flying platform, airship |
| sniper, assassin, terrorist | scout, tracker, phantom |
| ammunition, bullet, shell | energy cell, pulse shot, charge |

### Retry Strategy for Content Moderation Errors

When `generate_seedance_video` or `generate_image` returns a content moderation error:

1. **Identify the likely trigger** — check the prompt for any sensitive words from the table above
2. **Replace ALL sensitive words** with safe alternatives (don't just fix one)
3. **Soften action descriptions** — "character performs action" instead of violent verbs
4. **Add safety padding words** — "cartoon", "game character", "pixel art", "stylized", "fantasy"
5. **Retry with the modified prompt**
6. **If still blocked after 2 retries**: simplify the action drastically (e.g., "character stands in ready pose" instead of specific combat action)
7. **Log the experience**: add newly discovered trigger words to this table for future reference

### Per-Action Safe Prompt Patterns

For character animations that might trigger filters:

| Action | ❌ Risky Prompt | ✅ Safe Prompt |
|--------|----------------|---------------|
| shoot | "firing rifle with recoil" | "activating energy blaster with body recoil; no emitted flash/projectile" |
| hurt | "taking bullet damage, bleeding" | "knockback flinch, body recoils; no emitted sparks" |
| death | "getting killed, falling dead" | "deactivated, powering down, collapsing forward" |
| attack | "slashing enemy with sword" | "swinging energy blade forward; body motion only" |
| explode | "bomb explosion, debris flying" | "Do not generate as an asset; use the runtime explosion recipe" |

---

## Prompt Quality Checklist

Before submitting a prompt, verify:
- [ ] Art style is stated explicitly
- [ ] Background is specified: Seedance/i2v first frames use a solid pure `#FF00FF` (magenta) — or `#00F000` (green) if the subject contains magenta/pink — safe-color background filling the canvas edge-to-edge (never white or arbitrary high-contrast); transparent backgrounds appear only on FINAL static assets after the configured `rmbg2` or `color_key` matting backend; opaque/full-scene only for map or battle backgrounds
- [ ] For sprite actions: the prompt asks for one first-frame image, not a grid or sheet
- [ ] Static props, tiles, and UI are requested one asset per call (no grids)
- [ ] Subject is described with enough visual detail for consistency
- [ ] Seedance i2v prompt preserves first-frame composition, scale, and identity
- [ ] Containment and scale rules are included
- [ ] "No text, no labels, no UI" is stated
- [ ] View angle is specified
- [ ] Forbidden elements are listed (characters in backgrounds, objects in base maps, etc.)
