# Sprite & Asset Generation Guide

> One-stop manual: end-to-end generation flow for **static assets** (character portraits / props / UI / tilesets / effect icons) and **animated assets** (character idle/run/attack/cast, boss actions, and a projectile *body*). Everything goes through MCP tools — never call python subprocesses or HTTP directly. **FX / effect loops / impact / explosion are NOT i2v assets** — they are runtime Phaser `ParticleEmitter` recipes owned by the gameplay programmer and produce no image/video files.

---

## 0. Overall Flow

```
GDD §2 asset list
   │
   │  infer asset_type / action / view / art_style / runtime_format
   ▼
┌────────────────────────────────────────┐
│  Static asset (PNG)    Animated asset (spritesheet) │
│       │                      │           │
│       │                      │           │
│  §3 static image gen   §4 animation gen      │
│  (gpt-image-2 +        (i2v: first-frame +   │
│   configured matte)     Seedance + RMBG)     │
│       │                      │           │
│       └──────┬───────────────┘           │
│              ▼                            │
│       §5 QC + alpha validation           │
│              │                            │
│              ▼                            │
│       §6 Phaser load + Output layout     │
└────────────────────────────────────────┘
```

**Three core rules**:
1. **Pick one of the two safe colors (magenta `#FF00FF` or chroma green `#00F000`); no white / self-invented colors** — see §2.
2. **Generated stills go through `mcp__gca__produce_static_asset`; animated
   bodies go through the validated sprite tools** — use standalone generation
   or matting only for the documented per-stage exceptions, never direct
   services or local models.
3. **The i2v first_frame's visual inheritance ≫ the text prompt** — Seedance is an i2v model; it obeys the first frame's color/composition/character ten times more than the prompt.

### Performance: parallel generation + adaptive resolution

- Issue independent still calls together. For one sprite entity, finalize its
  single shared reference first, then issue every declared action together with
  that same `first_frame_override` and a disjoint action output directory.
  Candidate retries inside one action are serial; framing/content VLM checks
  within a candidate run with at most two workers.
- `gca.config.yaml` `assets.image.*`, `assets.matting.*`, `assets.seedance.*`,
  and `assets.sprite.*` are the authority for sizes, quality, static matting
  backend/thresholds, model, duration, resolution, retries, packing, and
  concurrency-sensitive defaults. Use tool schemas for legal size/aspect
  values; pass an override only when a GDD row or diagnosed failure requires it.

---

## 1. Parameters (inferred from GDD §2)

Don't make the user write frame counts / sizes; infer them from the GDD automatically:

- **asset_type**: `player` | `npc` | `creature` | `character` | `projectile` (a projectile *body* only)
- **action**: `idle` | `walk` | `run` | `jump` | `attack` | `cast` | `hurt` | `death` | `projectile` (for a projectile body)
- **view**: `side` | `topdown` | `3/4`
- **runtime_format**: spritesheet PNG + JSON (see §4); the transparent GIF is an intermediate/preview only
- **art_style**: `pixel_art` | `clean_hd` | `pixel_inspired` | `retro_pixel`

> This table is for animated bodies, not effects. Effects follow the
> profile/GDD strategy and only declared flipbooks use file-backed animation.

### Asset Plan Inference (derive the action bundle by genre)

The GDD §2A/§2B rows are the complete action bundle; do not infer extra actions
from genre. All actions for one entity share one reference. Projectile-body
rows use `animation_profile:"projectile"`; trails, impacts, and explosions stay
in the separate GDD §5 effect strategy.

---

## 2. Safe Color Selection (mandatory rule)

Hard-won lesson: any "self-invented" safe color (white / blue / gray / any near-chromatic color) produces at least one of two problems under the chroma-key + RMBG dual pipeline:

- **(A) doesn't matte cleanly** — a pure color isn't far enough from the foreground and eats edge detail
- **(B) mattes out the foreground** — regions of the foreground close to the background color get removed too

**White is worst**, because it's an RGB extreme corner and the foreground almost always has near-white pixels.

**You can only choose one of these two**:

| Safe color | hex | When to use |
|---|---|---|
| `magenta` | `#FF00FF` | **most foregrounds** (no magenta / strong pink / hot-pink pixels) — this INCLUDES **green** subjects (foliage / slime / grass armor / green clothing), which are far from magenta |
| `chroma green` | `#00F000` | foregrounds with **magenta / pink / hot-pink pixels** (which conflict with the magenta key), so the farther safe color is green |

> ⚠️ The safe color is the one **farther** from the foreground, NOT the one that matches it. A **green** subject must use **magenta** — green art on a green key gets matted out. Follow the distance rule below (pick the LARGER distance), never the subject's own color.

**Choose through MCP**: pass `reference_image` to
`mcp__gca__build_i2v_prompt` so the tool applies its active safe-color
selection and validation. To force one of the two documented colors, use
`preset="magenta_bg"` or `preset="green_bg"`. Do not run local color-analysis
scripts.

> ❌ Forbidden: `bg_hex="#FFFFFF"` / `bg_hex="#0000FF"` / any self-invented color — `mcp__gca__build_i2v_prompt` will raise `INVALID_CONFIG`.

### Effect colors must be far from the bg (not just far from the foreground)

Cyan effects succeed in the magenta pipeline; using a green bg + cyan effects fails — cyan isn't far enough from green and gets eaten by the chroma-key/color-aug.

---

## 3. Static Assets — image generation + matting

Applies to character references, props, UI elements, tiles, and other
single-frame PNGs. Read `prompt-rules.md` for prompt content and
`postprocess-guide.md` § "Single-Image Background Removal" for manual legacy
exceptions.

### 3.1 Reference generation (identity-sensitive characters)

Generate one validated reference before any action. When the GDD row has a Ref
Crop, pass it to `produce_validated_first_frame`; otherwise use the text-only
route. Reuse the same final image and canonical character description for every
action through `first_frame_override`, retaining the sibling `verdict.json`.
Never generate an action-specific identity frame.

### 3.2 General static assets (prop / UI / tileset pieces)

Use one `mcp__gca__produce_static_asset` call per declared still. Pass
`ref_image` when the row has a crop, the exact manifest output path and
`asset_type`, the profile-authorized target sizing, binding geometry when
applicable, and `collidable:true` when a bbox is required. Opaque
background/map/parallax rows use `skip_matting:true`; every branch seals the
same-basename `.asset.json`. Defaults come from `gca.config.yaml`.

### 3.3 Static matting — configured backend

`produce_static_asset` performs matting and trim internally. The default
`assets.matting.backend:auto` prefers RMBG-2.0 and automatically uses the
deterministic `#FF00FF` / `#00F000` route for a complete asset when RMBG has a
network, missing-route HTTP 404, or HTTP 5xx outage. Explicit `rmbg2` and
`color_key` remain available. Auto mode probes each endpoint once per
MCP process and remembers the result: after a fallback-eligible outage, later
static and video assets go directly to color-key without probing RMBG again. Use
standalone `mcp__gca__rmbg2_matting` or `mcp__gca__color_key_matting` only for
an existing keyed intermediate that needs per-stage control, then run
`trim_transparent` and any required uniform scale or bbox. Use the active
ToolDef/config values; never call the service, local model, or bundled runners
directly.

---

## 4. Animated Assets — i2v + matting

Applies to character/boss/projectile bodies shipped as spritesheet PNG + JSON.
Effects are separate and follow the profile contract.

### 4.1 Full pipeline

```
generate_image (character on magenta/green safe-color background)
   │
   │  §3.1 generate the reference — it IS the first_frame
   │  all actions of the same character share this image
   ▼
reference.png (RGB, magenta background, used directly as first_frame)
   │
   │  4.3 build_i2v_prompt (user content + validated constraint block)
   │      uploaded to the configured public R2 bucket
   ▼
mcp__gca__generate_seedance_video (first_frame = reference.png)
   │
   ▼
mp4 (animation video with a "safe-color" background)
   │
   │  4.5 mcp__gca__rmbg2_video_matting
   │      frames sent to RMBG-2.0 in parallel + optional color-aug rescue
   ▼
transparent_frames/ (PNG sequence) + transparent.gif
   │
   │  4.6 GIF → Spritesheet conversion (final step)
   │      pack the frame sequence into a horizontal spritesheet + generate metadata JSON
   ▼
[action]-sheet.png + [action]-sheet.json (loaded directly by Phaser)
```

> **Phaser 4 cannot play GIFs directly; you MUST convert to a spritesheet to use them in-game.**

> **The reference generated in §3.1 is already an RGB PNG on a magenta background — no extra compositing needed; use it directly as the Seedance first_frame.**

### First-Frame Rules

- When generated in §3.1, the prompt includes "Plain solid pure magenta (#FF00FF) background", so the output is a usable first_frame
- All actions of the same character share one reference (hard rule)
- Once that reference is finalized, issue the entity's independent action calls
  together; do not serialize idle/run/attack generation behind one another
- Keep the character at runtime scale; don't fill the canvas
- Keep the feet / body anchor stable with the **feet resting ON the bottom edge (0–4px margin)**; leave safety margin only on the **TOP / LEFT / RIGHT** for i2v swing arcs — there is no space below the feet for grounded characters
- **Do not** include weapon trails / projectiles / slash arcs / UI / labels / multiple poses

### 4.3 build_i2v_prompt — user content + validated constraint block

Constraint-block origin: converged over 5 rounds of a VLM feedback loop on `frost_druid_magenta_iter`, then baked into [`adapters/seedance.py`](../../../../../gca-mcp/src/gca_mcp/adapters/seedance.py) (`build_i2v_prompt` + `I2V_PROMPT_PRESETS`), exposed via `mcp__gca__build_i2v_prompt`.

```python
prompt_obj = mcp__gca__build_i2v_prompt(
    user_content="A frost druid casting an ice spell, robes flowing, side view.",
    preset="magenta_bg",                     # = bg_hex='#FF00FF' + cool/blue effect_palette
    # foreground_recolor_instructions="...", # only when foreground is < 100 distance from the bg
    reference_image="assets/sprites/[key]/[action]-first-frame.png",  # auto-picks bg_hex
)
```

The constraint block includes:
- background must stay pure and unchanged
- foreground must not approach the background color
- effects must be **fully opaque / equally bright / limited to a palette**
- 14 STRICTLY AVOID items (transparent particles / fade in-out / low opacity / glow halo / bloom / light flare / hazy / wispy / foggy / ...)

### Character Framing Hard Constraints (framing rules)

`build_i2v_prompt` bakes the following rules as STRICTLY AVOID extensions; seedance must not deviate:

1. **Feet must always touch the bottom edge of the video** — during walk / run / idle / attack / cast, the feet always touch the canvas bottom; never floating / hovering / standing mid-frame
2. **A jump may briefly leave the bottom edge** — but must land back on it before the loop ends
3. **The character must not leave the screen** — no frame of the head / body / weapon / accessories / hair / tail / cape may cross any of the four canvas edges
4. **Composition stays fixed** — no zoom / pan / dolly / recomposition; every frame matches the first_frame's composition

Why these are hard constraints:
- the matted transparent GIF / frame sequence is dropped directly into the game as a sprite; the game side defaults the anchor to bottom-center, so drift = "falling through the floor"
- one frame with the character off-screen = one missing frame in the sprite sheet = a broken animation
- recomposition shifts pixel positions between frames, so even perfectly matted frames jitter once stitched together

### 4.4 Seedance i2v call

```python
mcp__gca__generate_seedance_video(
    prompt=prompt_obj["prompt"],
    output="assets/sprites/[key]/[action].mp4",
    first_frame="assets/sprites/[key]/[action]-first-frame.png",  # local path uploads to R2
    # duration/resolution/ratio/model use assets.seedance.* / assets.sprite.*
)
```

**Seedance parameter rules**:
- Use the current ToolDef for legal model, resolution, and ratio values.
- Let `gca.config.yaml` supply defaults; override only for a declared asset need.
- prompt safety: "cartoon game character" / "looping walk cycle" / "no attack" / "no weapon swing" / "preserve original appearance"
- content-moderation fallback: if it returns `OutputVideoSensitiveContentDetected`, simplify the first frame, remove weapons/attack poses, and change the action prompt

The returned JSON contains `video_url` (upstream video URL) and `model_id`; persist them yourself when you need traceability.

### 4.5 Video matting — `mcp__gca__rmbg2_video_matting`

Adapter: [`gca-mcp/src/gca_mcp/adapters/rmbg2_video.py`](../../../../../gca-mcp/src/gca_mcp/adapters/rmbg2_video.py), reuses the single-frame service from §3.3.

```python
mcp__gca__rmbg2_video_matting(
    input="assets/sprites/[key]/[action].mp4",
    output_dir="assets/sprites/[key]/",
    formats=["gif"],
    # safe-color/matting defaults come from gca.config.yaml
)
```

The active ToolDef and `assets.rmbg.*` / `assets.sprite.*` config own matting
presets, thresholds, workers, and output limits. Override them only to address
a diagnosed asset-specific failure.

**Multi-format output**:

```python
mcp__gca__rmbg2_video_matting(
    input="vid.mp4",
    output_dir="out/",
    formats=["gif", "webm", "mov", "mp4_preview"],
    bg_hex="#FF00FF",
    # matting defaults come from gca.config.yaml
)
```

| format | For | Encoder |
|--------|------|------|
| `gif` | Preview / intermediate ONLY — 1-bit transparency (Phaser 4 does NOT play GIFs; pack a spritesheet for in-game use, §4.6) | Pillow global palette |
| `webm` | Modern browser video (VP9 + alpha) | libvpx-vp9, yuva420p |
| `mov` | Editing software (QuickTime RLE lossless) | qtrle |
| `mp4_preview` | Black-background preview (mp4 has no alpha) | mpeg4 |

### 4.6 Spritesheet assembly — use `mcp__gca__pack_spritesheet`

The final step for every animated action: always call `mcp__gca__pack_spritesheet` to assemble the matted frame directory into a Phaser spritesheet PNG + JSON. **Do not hand-write Python/Pillow assembly** — the tool wraps all the logic internally and automatically respects the GPU `MAX_TEXTURE_SIZE` cap.

```
mcp__gca__pack_spritesheet({
  frames_dir: "assets/sprites/player/idle/video/final/rmbg2/transparent_frames",
  output: "assets/sprites/player/idle-sheet.png",   // .json written next to it, same name
  frame_width: 128, frame_height: 128,              // GDD Frame Size
  // optional, defaults read from assets.sprite.*: fps / max_sheet_width / max_frames / min_frame_dimension_px
  // optional, write metadata too: facing / content_bbox (from analyze_sprite_metadata)
})
```

The tool reads packing defaults from `gca.config.yaml` `assets.sprite.*`,
normalizes frames to the GDD frame envelope, caps/wraps within configured
limits, and writes `frameWidth/frameHeight/frameCount/columns/rows/fps` plus
sampling and supplied facing/bbox metadata. Treat the tool output as
authoritative; do not reproduce its packing algorithm.

---

## 5. Quality Control (after generating each action)

1. **First frame**: matches reference identity / scale / composition
2. **Video**: action is readable, loop is acceptable, no unexpected zoom / crop
3. **Transparency**: overlay the transparent GIF on a contrasting background and verify there is no white/magenta halo
4. **Runtime scale**: the character size is consistent with other actions and collision-box expectations
5. **Safety failures**: if Seedance returns `OutputVideoSensitiveContentDetected`, simplify the first frame and prompt

Use `mcp__gca__validate_image` and the validated composite returns for alpha and
quality evidence; do not run local image-inspection scripts.

### slot-fit validation (asset ↔ design slot) — ⛔ intermediates go to a dedicated path, don't pollute `assets/`

`validate_image` / `validate_asset_video` only check an asset against **its own generation prompt**, so they can't catch "the image is clean, but it's simply not the asset this slot should hold" (e.g. GDD §2 says `boss_mech` is a "four-legged walker tank" but the generated result is a humanoid mech — both are clean; only comparing against the **design slot** catches it).

After every asset assigned to one worker is finalized (sprite:
`pack_spritesheet` done; static: atomic production done), submit the complete
set in **one** `items` call. The tool starts one worker per item with no smaller
concurrency cap. Each item is checked against the **GDD §2 slot description for
that key** (design intent, **not** its generation prompt). For a spritesheet,
the tool crops frame zero internally for visual judgment and binds the receipt
hash to the complete sheet.

```
mcp__gca__validate_asset_slot({
  items: [
    {
      image: "assets/sprites/<key>/idle-sheet.png",
      action: "idle",
      slot_key: "<key>"
    },
    {
      image: "assets/ui/<ui-key>.png",
      slot_key: "<ui-key>"
    }
  ]
})
```

A sibling `reference.png` cannot publish the receipt. The canonical S1-S5 rubric
is immutable; callers may append `extra_rubrics` but cannot replace it. The
schema-v3 receipt records logical key, action, runtime path and artifact hash,
canonical rubric version/hash, slot-contract hash, the selected GDD slot-section
context, pinned Doubao account, request timing, and validation-policy hash. An
unchanged passed receipt is reused without a new VLM request; copying obsolete
description text into a changelog does not preserve freshness.

**All intermediates for this must be written to the dedicated "validation intermediates" directory `.playtest/verify_middle/`, not scattered around, not into the `assets/` shipped tree, and not mixed with `verify/` (the finalized verification outputs: recording / play / alignment / critic / assets …)**:

```
<GAME_DIR>/.playtest/verify_middle/          ← dedicated root for validation "intermediates", separate from verify/, zero overlap with other flows
├── slot/
│   ├── <key>.json        ← (b) per-entity slot-fit verdict (verdict / passed / failures + raw_response); the gate reads this
│   ├── <key>.<action>.frame.png ← tool-owned frame-zero crop for sheet judgment
│   └── ...               ← one separate file per entity, so parallel assets-agents don't overwrite each other
└── scale/scale-report.json  ← (a) where the proportion/collision gate gateEntityScale writes: per-entity render size + each verdict (written by the gate automatically; assets-agent needn't touch it)
```

- Always land in `<GAME_DIR>/.playtest/verify_middle/slot/` (this is the home for validation **intermediates**), **not** in `assets/` (the shipped runtime tree), `verify/` (finalized verification outputs), or a random temp dir — things under `.playtest/` don't go into the game bundle, things under `assets/` do.
- The paths are **registered in MCP path management**: the root constant `VERIFY_MIDDLE_DIR` (=`verify_middle`) and the slot subdir constant `SLOT_VALIDATION_DIR` (=`verify_middle/slot`), mirrored in `gca-mcp/src/gca_mcp/playtest_layout.py` + `gca-mcp/src/gca_mcp/runners/verify/playtest-layout.mjs`; `gateSlotValidation` also reads it. **Hang any new validation intermediate under `verify_middle/` and register the constant in these two layouts first — don't hardcode it elsewhere and don't reuse another flow's directory.**
- FAIL (`passed:false`) = the asset deviated from the design slot: regenerate within the normal best-of-N / retry budget (re-seed from the `Ref Crop`, nudge the prompt toward the §2 description) and re-validate; even when the budget is exhausted, **write the FAIL in** and record it in the returned `issues[]` — never overwrite it with a PASS. Both `iteration_ready` and `ready_to_ship` require a current receipt for every sprite entity and block on any FAIL, preventing state.`first_pass_complete`.
- Every `sourceStrategy:"generated-image"` manifest entry must receive a current receipt. Procedural/no-file effects do not enter the call because they have no generated artifact.

---

## 6. Phaser Loading

```javascript
// preload(): W/H/FPS come from [action]-sheet.json
this.load.spritesheet('dino-walk', 'assets/sprites/dino/walk-sheet.png', { frameWidth: W, frameHeight: H });

// create(): build an animation from the sheet frames and play it, anchored at the feet
this.anims.create({
  key: 'dino-walk',
  frames: this.anims.generateFrameNumbers('dino-walk'),  // frameCount/fps per walk-sheet.json
  frameRate: FPS,
  repeat: -1,
});
const s = this.add.sprite(x, y, 'dino-walk').setOrigin(0.5, 1);  // anchor at bottom-center of the feet
s.play('dino-walk');
```

Phaser 4 **cannot** play GIFs — always load the `[action]-sheet.png` + `[action]-sheet.json` produced by §4.6; the transparent GIF is an intermediate/preview only.

---

## 7. Output Structure

**Single action**:

```
assets/sprites/[asset-key]/
├── [action]-first-frame.png             ← still frame for i2v
├── [action].mp4                         ← Seedance output / backup
├── [action].seedance-response.json      ← API response, traceability
├── [action]-transparent.gif             ← intermediate/preview only (NOT loaded at runtime)
├── [action]-sheet.png                   ← LOAD THIS in Phaser (runtime format)
├── [action]-sheet.json                  ← LOAD THIS in Phaser (runtime format — frameWidth/frameHeight/frameCount/fps)
└── [action]-frames/
    └── transparent/
        ├── frame-0000.png
        ├── frame-0001.png
        └── ...
```

**Hero action bundle**:

```
assets/sprites/hero/
├── idle-first-frame.png
├── idle-sheet.png / idle-sheet.json       ← LOAD THIS in Phaser (runtime format)
├── run-first-frame.png
├── run-sheet.png / run-sheet.json
├── jump-first-frame.png
├── jump-sheet.png / jump-sheet.json
├── attack-first-frame.png
└── attack-sheet.png / attack-sheet.json
```

**Validation intermediates — separate from the `assets/` shipped tree and from the `verify/` output tree**: the slot-fit (§5) verdicts / raw VLM replies / frame snapshots do **not** go into `assets/`; they all land in the dedicated root `.playtest/verify_middle/` (`.playtest/` is not bundled into the game):

```
<GAME_DIR>/.playtest/verify_middle/        ← dedicated root for validation "intermediates" (constant VERIFY_MIDDLE_DIR), zero overlap with other flows
├── slot/<key>.json                       ← (b) one per entity; the gate gateSlotValidation reads it; constant SLOT_VALIDATION_DIR
└── scale/scale-report.json               ← (a) where gateEntityScale writes; constant SCALE_REPORT
```

> Convention: `assets/` = **shipped** artifacts loaded/bundled at runtime; `.playtest/verify/...` = finalized **verification outputs** (read by report/gate/critic); `.playtest/verify_middle/...` = **intermediates** of the validation process. Keep all three strictly separate — don't write validation artifacts into `assets/`, and don't mix them with the `verify/` flow.

---

## 8. End-to-End Examples

Do not reproduce this workflow with PIL, NumPy, ffmpeg, HTTP, or local model
scripts. A static example is one `produce_static_asset` call. An animated
example is one shared validated reference, one
`produce_validated_sprite_action` call per declared action, and one
`pack_spritesheet` call per usable result. The earlier sections define the
arguments and outputs; `gca.config.yaml` supplies defaults.

---

## 9. Key Takeaways (from frost_druid + RMBG measurements)

1. **first_frame compositing ≫ text prompt** — a 10× gap in color control
2. **VLM iteration has limited ROI** — 5 rounds cut violations from 3 to 2, but the prompt ballooned to 3591 chars and **actually introduced motion perturbation** (iter_05 RMBG flip avg=4044 vs single strict=1222)
3. **server-side color-aug rescue ≫ threshold tuning** — RMBG's bimodal output (95% of pixels at 0/255) means tuning the threshold doesn't help; color-aug rescuing peripheral effects by color distance is the only effective method
4. **color-aug is nearly useless on white-bg scenes** — white is an RGB extreme corner, so the inverse logic covers too broad a range; another confirmation of why white can't be a safe color
5. **production pipeline**: user-content prompt + baked constraints + nearly-off color-aug is the sweet spot

---

## 10. Common Mistakes Checklist

- ❌ **Using a grid / sprite sheet to generate animation frames** — this is the most severe mistake. The multi-frame grid images `generate_image` produces are just static poses, not real animation. All character/enemy/boss/projectile-**body** animation **must** go through the i2v pipeline: `produce_validated_sprite_action` or a manual first-frame → Seedance → rmbg2_video_matting. **FX / impact / explosion do NOT go through i2v** — they are runtime `ParticleEmitter` recipes (no image/video files)
- ❌ Using `background="transparent"` to make gpt-image-2 output a transparent PNG directly — unstable for some themes, leaves semi-transparent edge artifacts
- ❌ Using white / blue / gray as the first_frame safe color — guaranteed to trigger (A) doesn't matte cleanly or (B) mattes out the foreground
- ❌ Writing "transparent / magenta background" in the prompt and hoping the model obeys — Seedance is an i2v model; it looks at the image, not the words
- ❌ Asking the model to output an "action sheet / sprite sheet" for the reference — a single reference PNG is enough; actions go through i2v
- ❌ Stuffing weapon trails / multiple poses / UI / labels into the first_frame — makes seedance think these are elements to preserve
- ❌ Loading `*-frames/raw/*.png` or the staging composite directly — Phaser 4 loads the `[action]-sheet.png` + `[action]-sheet.json` spritesheet from §4.6 (the transparent GIF / `*-frames/transparent/*.png` are intermediates/preview only, never the in-game asset)
- ❌ Loading `assets/sprites/raw/*.png` directly — that's the pre-matting staging
- ❌ Loading the mp4 Seedance returns directly — that's the staging video; it must go through §4.5 RMBG

---

References:
- Seedance adapter: [`gca-mcp/src/gca_mcp/adapters/seedance.py`](../../../../../gca-mcp/src/gca_mcp/adapters/seedance.py)
- RMBG adapter: [`gca-mcp/src/gca_mcp/adapters/rmbg2.py`](../../../../../gca-mcp/src/gca_mcp/adapters/rmbg2.py) / [`rmbg2_video.py`](../../../../../gca-mcp/src/gca_mcp/adapters/rmbg2_video.py)
- RMBG service wrapper: [`services/rmbg2_server.py`](../../../../../services/rmbg2_server.py)

Historical VLM iteration logs and measurement artifacts are not checked into this
repository; keep this section limited to live, repo-local implementation links.
