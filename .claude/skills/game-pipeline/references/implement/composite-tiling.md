# Composite & Seamless Tiling (linear extendable assets)

Patterns for any asset that is **built by joining pieces** — platforms, pipes,
bridges, walls, fences, ground strips. AI-generated pieces are made
independently, so they have different heights, cap widths, and edge pixels.
Placing them with ad-hoc `add.image` calls makes them look "dumped together":
oversized caps, stepped tops, broken seams. Two layers fix this — **generation**
(make the middle tile truly repeatable) and **implementation** (compose
deterministically). Use both.

---

## Layer 1 — Generation: make the middle tile seamless

A "repeatable middle" piece (`platform-mid`, `pipe-mid`, `wall-mid`, ground tile)
is almost never tileable straight from the generator: butting two copies shows a
hard seam. Run the deterministic, CPU-only offset-blend pass **after** matting,
before the asset is used:

```python
mcp__gca__postprocess(
    command="make-seamless",
    input="assets/props/platform-mid.png",
    output="assets/props/platform-mid.png",   # overwrite in place
    feather=16,                               # cross-fade band half-width (px)
    # both_axes=True,                         # for ground/wall tiles repeated in 2D
)
```

This rolls the tile by half so the original wrap-edge seam lands in the centre,
then cross-fades a feather-width band over it — the result repeats cleanly under
`TileSprite`/texture-repeat. End **caps** are NOT run through this (they are not
repeated); only the middle/repeatable piece is.

Rules:
- Run `make-seamless` on every `tileset_piece` / repeatable-middle asset.
- Use `both_axes=True` for tiles repeated in both X and Y (floor/wall fields);
  horizontal-only (default) for platform/pipe middles.
- `feather` ≈ 8–24 px. Wider = smoother seam but more of the tile edge is blended;
  keep it well below half the tile width.

---

## Layer 2 — Implementation: deterministic composition

Never place `left/mid/right` (or pipe segments) with three guessed `add.image`
positions. Scale all pieces to one uniform display size, butt them with
`setOrigin(0,0)` + cumulative offset, and fill the extendable span with a
`TileSprite` of the (now seamless) middle. The canonical helper is
`buildPlatform()` in `platform.md` — the same shape works for pipes, walls,
and bridges (swap the texture keys and the major axis).

Key invariants (enforced by the helper, not by hand-placement):
- **Uniform size** — every piece scaled to the same target height/width, so a
  cap can never out-size the middle.
- **Seam continuity** — `setOrigin(0,0)` + cumulative offset: each piece starts
  exactly where the previous ends. No gaps, no overlaps.
- **Tiled middle** — `TileSprite` repeats the seamless middle to any length,
  instead of stretching one image or stacking copies.
- **Single collision body** — collision is one rectangle on the standable/solid
  surface, decoupled from the multi-piece visual.

See `platform.md` → "Composite Platform from a 1×3 Strip" for the reference
implementation.

---

## Layer 2b — Strip + bbox form (single sheet, `cells[].bbox`)

When the pieces ship as **one sheet** split into frames with per-cell `bbox`
metadata (e.g. `tiles-rooftop.png` + `cells: [{key:'left', bbox}, …]`) instead
of three independent PNGs, two extra traps appear — both observed in real
generated games. The frame is a fixed grid cell (e.g. 128px); the actual art
inside it is **not** frame-sized and **not** frame-aligned.

**Trap 1 — align by EACH cell's own `bbox.y`, never a shared one.**
The three cells almost always have different content-top offsets inside their
frame (real case: left `bbox.y=44`, mid `70`, right `59`). Placing all frames at
one offset (`surfaceY - midBbox.y`) steps the caps above/below the mids even
though the frames line up. Compute the screen position **per cell**:

```javascript
const cells = CONFIG.sprites['tiles-rooftop'].cells;
// sprite origin is center → centerY = (surfaceY - cell.bbox.y) + frameH/2
const frameCenterY = (i) => surfaceY - cells[i].bbox.y + frameH / 2;
this.add.sprite(cx, frameCenterY(0), key, 0); // left
this.add.sprite(cx, frameCenterY(1), key, 1); // mid
this.add.sprite(cx, frameCenterY(2), key, 2); // right
```

**Trap 2 — step by CONTENT width (`bbox.w` / `bbox.x`), never frame width.**
The art does not touch the frame edges — each cell has transparent margins
(`bbox.x` on the left, `frameW - bbox.x - bbox.w` on the right). Stepping by the
full frame width (`x += 128`) leaves those two margins as a transparent **gap**
between pieces (real case: 12px right-margin + 1px left-margin = ~13px seam).
Advance so each piece's **content edge** butts the previous piece's content edge:

```javascript
// place piece i so its bbox.x lands right after the previous content's right edge
nextContentLeft = prevContentRight;            // continuity
spriteX = nextContentLeft - cells[i].bbox.x;   // back out the frame origin
prevContentRight = spriteX + cells[i].bbox.x + cells[i].bbox.w;
```

> **Generation-side prerequisite.** Both traps are *mitigations*; they cannot fix
> a strip whose three cells have mismatched **content heights** (real case: caps
> 110/116px tall, mid only 56px) — that body-size mismatch is baked into the art.
> A strip meant to tile must be generated with a shared interface contract:
> equal content height, equal connecting-face height, a horizontally-seamless
> middle, and cap inner-faces pixel-aligned to the middle's faces.
