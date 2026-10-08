"""Seedance 2.0 i2v / t2v adapter.

Also hosts the i2v prompt-template helper that wraps user content with
the iter_05-validated constraint block (background lock, hard-edged
solid effects, no glow / fade / haze, etc.). Exposed as the
``build_i2v_prompt`` MCP tool and reused inside ``generate_video`` when
the caller passes ``i2v_template=True``.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from ..config import AssetSeedanceDefaults, Config
from ..errors import ToolError, is_transient_seedance_error
from ..providers import get_provider

# -------------------- i2v prompt template --------------------


_EFFECTS_BLOCK = """
CRITICAL EFFECTS RULES:
- If the action naturally produces visible effects (muzzle flash, spell cast, impact sparks, etc.), render them as described in the user content above.
- ALL effects MUST be FULLY OPAQUE and SOLID — every effect is a hard-edged solid shape with crisp edges.
- Effects MUST have constant opacity throughout the entire video (NO fading in, NO fading out, NO pulsing).
- Effect colors strictly limited to: {palette_str}. Never the background color, never any color close to it.
- Effects must be visually distinguishable from BOTH the {bg_hex} background AND the character body.
"""

_NO_EFFECTS_BLOCK = """
CRITICAL: NO EFFECTS — BODY ANIMATION ONLY:
- Do NOT add any visual effects: no muzzle flash, no sparks, no glow, no particles, no magic, no projectiles, no energy.
- The character performs ONLY the described body motion — nothing else appears or disappears.
- NO shooting, NO attacking, NO weapon firing unless EXPLICITLY described in the user content above.
"""

# Projectile-BODY no-FX block. A projectile IS the subject, so — unlike the
# character block above — it must NEVER say "no projectiles" (that would forbid
# the very thing being animated). It forbids only the SEPARATE runtime VFX
# (muzzle flash, sparks, glow halos, impact bursts) that are produced by the
# game's ParticleEmitter at play time, not baked into the sprite sheet.
_PROJECTILE_NO_EFFECTS_BLOCK = """
CRITICAL: NO ADDED EFFECTS — PROJECTILE BODY ONLY:
- Animate ONLY the projectile body itself (its flight, spin, or pulse). Do NOT add any SEPARATE emitted effects around it: no muzzle flash, no impact sparks, no glow halo, no smoke cloud, no energy field, no secondary particles.
- Do NOT spawn, launch, or fire any additional projectile — there is exactly ONE projectile body and nothing else appears or disappears.
- An intrinsic, tightly-attached tail that is part of the body's own art (e.g. the flame of a fireball) may move with it, but no detached trailing particle stream.
"""

CANONICAL_CONSTRAINT_TEMPLATE = """{user_content}

The reference first frame shows the EXACT background color we want — pure {bg_hex}.

CRITICAL BACKGROUND RULES:
- Keep the {bg_hex} background COMPLETELY UNCHANGED in every frame, exactly as in the first frame.
- Do NOT add gradient. Do NOT darken. Do NOT add shadows on background.

CRITICAL FOREGROUND RULES:
- Keep character's original color palette and identity unchanged.
- NO part of the character may approach the background color {bg_hex}. Every pixel of the character must be visibly different from {bg_hex}.
{recolor_block}
CRITICAL SUBJECT MARGIN (HARD — never violate, applies to EVERY frame):
- GROUNDED FRAMING (asymmetric on purpose): the character stands ON the bottom edge, with empty {bg_hex} margin only on the TOP, LEFT, and RIGHT. The character (and every held weapon / prop / cape / hair / particle trail) MUST stay inside the region bounded by ≥8% empty {bg_hex} margin on the TOP, LEFT, and RIGHT edges. The BOTTOM edge is the ground line — the feet touch it (see framing rules) — so there is NO bottom margin.
- The character's bounding box CENTER stays within ±5% of the canvas center horizontally. The character does NOT drift toward an edge, does NOT walk off-screen, does NOT shrink/grow toward an edge.
- The reference first frame already obeys this framing — preserve that composition exactly. Do NOT recompose the character closer to any edge, do NOT zoom in, do NOT crop.
- For ANY motion (swing, dash, cast, kick, jump, projectile launch): the FARTHEST point of the motion (tip of weapon, tip of kick, head at apex of jump, leading edge of projectile, end of cape flutter) MUST still be ≥8% inside the TOP, LEFT, and RIGHT edges (the feet may reach the bottom edge). If a planned motion would exceed that margin, REDUCE the motion amplitude until the entire arc / extension fits.

CRITICAL FRAMING RULES (HARD CONSTRAINTS — never violate):
- HARD — CAMERA FROZEN (highest priority framing rule): This is a locked-off static camera. The character's on-screen bounding-box HEIGHT MUST stay within ±3% of the first frame at EVERY frame — never re-draw the character bigger or smaller on screen. The empty background margin on the TOP / LEFT / RIGHT edges MUST stay identical to the first frame; if the top / left / right margin shrinks at any frame, the camera has zoomed in — which is FORBIDDEN. Treat the first frame's framing as a fixed, unchangeable crop: same character on-screen size, same margins, same composition for the entire video. This applies even during big actions (punch, dash, kick) — the pose changes, the framing does NOT.
- The character's feet MUST stay anchored at the bottom edge of the video frame at every moment of the video. The bottom of the lowest foot/paw/leg MUST touch the bottom edge of the canvas.
- During walking / running / idle / attack / cast: feet remain in contact with the bottom edge — NO drifting upward, NO floating, NO standing in mid-air.
- During jumping: feet may briefly lift off the bottom edge for the apex of the jump, but the character MUST land back with feet on the bottom edge before the loop ends.
- The character MUST NEVER exit the video frame. No part of the body, weapon, accessory, hair, tail, or cape may cross any of the four edges of the canvas at any frame.
- HARD ON-SCREEN CONSTRAINT (applies during ALL motion — swings, casts, dashes, jumps): the FULL arc / trajectory / extension of every moving part — sword swing arc, staff sweep, spell ray, projectile path, kick extension, cape flutter, particle trail, magic effect — MUST stay entirely inside the canvas at every single frame, AND must still respect the 8% margin on the TOP, LEFT, and RIGHT edges defined above. Plan the motion amplitude so the FARTHEST point of any swing / extension / projectile is still ≥8% inside the top, left, and right edges (feet may reach the bottom edge). If a planned swing would cross the top/left/right 8% margin (let alone the canvas edge), REDUCE the swing amplitude until it fits — never let any part enter that margin band, even for a single frame.
- Effect projectiles and particle trails (sparkles, bubbles, slashes, fireballs, magic rays) MUST also stay fully inside the canvas. Do not let them fly off the screen or trail past any edge.
- Do NOT zoom, pan, or recompose. Keep the SAME framing, character size, and canvas composition as the reference first frame for every single frame of the video.
{effects_block}
STRICTLY AVOID (these issues were observed in failed attempts and must NOT recur):
- Any effect that fades in or fades out at any point of the video.
- Any effect with gradually increasing or decreasing opacity over time.
- Any effect that pulsates in opacity.
- Faint or low-opacity sparkle, particle, or glow effects of any size.
- Soft luminous low-opacity effects of any kind.
- Semi-transparent particles, sparkles, or glow halos through which the background is visible.
- Strong overexposed bloom glow on light-emitting items (staff tips, crystals, weapons, eyes, etc.) that washes out detail.
- Strong starburst light flares of any size.
- Hazy, wispy, misty, foggy, or ghostly effects of any kind.
- Any "magical aura" or "energy field" rendered as semi-transparent or translucent.
- Any color spill from the background onto the character or effects.
- Character drifting upward or floating above the bottom edge of the canvas.
- Camera zoom-in, zoom-out, pan, dolly, or any change in framing across frames.
- The character growing or shrinking on screen (bounding-box height changing more than ±3% from the first frame) — this reads as a camera zoom and is forbidden.
- The background margin on the top / left / right edge shrinking relative to the first frame (a tell-tale sign of an unwanted zoom-in).
- Any frame where the character (or any part of it) is partially or fully off-screen.
- Any frame where the character (or any part of it) touches or crosses the 8% margin band on the TOP, LEFT, or RIGHT edge (the feet resting on the bottom edge is REQUIRED, not a violation).
- Sword tip, staff tip, spear tip, or any weapon extension crossing any canvas edge during a swing, thrust, or sweep arc.
- Any limb (arm, leg, foot, kick, punch) extending past any canvas edge during an attack, dash, or motion.
- Cape, hair, tail, scarf, or cloak edges crossing any canvas edge during motion.
- Magical projectiles, sparkles, bubbles, slashes, fireballs, lightning rays, or any particle effect flying off-screen or trailing past any canvas edge.
- Any single frame in which any part of the character or any effect is clipped by the top, bottom, left, or right edge.
"""


# Free-flying PROJECTILE constraint template. A projectile body (bullet / arrow
# / fireball / bolt) is NOT a grounded character: it has NO feet, does NOT stand
# on the bottom edge, and must stay CENTERED with margin on ALL FOUR edges — the
# exact opposite of the grounded framing above. Injecting the character template
# for a projectile gave the model mutually exclusive instructions (stand on the
# bottom edge + feet anchored) that directly contradict the projectile validator
# (PR1: "must NOT rest on or touch the bottom edge"). This template uses
# projectile-body language only — no feet, no limbs, no character.
PROJECTILE_CONSTRAINT_TEMPLATE = """{user_content}

The reference first frame shows the EXACT background color we want — pure {bg_hex}.

CRITICAL BACKGROUND RULES:
- Keep the {bg_hex} background COMPLETELY UNCHANGED in every frame, exactly as in the first frame.
- Do NOT add gradient. Do NOT darken. Do NOT add shadows on background.

CRITICAL FOREGROUND RULES:
- This subject is a FREE-FLYING PROJECTILE body (bullet / arrow / fireball / bolt / orb) — NOT a character, NOT a figure with feet or limbs. Animate ONLY the projectile body.
- Keep the projectile's original color palette, shape, and identity unchanged.
- NO part of the projectile may approach the background color {bg_hex}. Every pixel of the projectile must be visibly different from {bg_hex}.
{recolor_block}
CRITICAL SUBJECT MARGIN (HARD — never violate, applies to EVERY frame):
- CENTERED FRAMING: the projectile body (and its intrinsic attached tail, if any) MUST stay CENTERED inside the canvas, with empty {bg_hex} margin of ≥8% on ALL FOUR edges (top, bottom, AND left, AND right).
- The projectile does NOT rest on, touch, or approach the bottom edge — it is a free-flying object with clear background beneath it at every frame. Standing / grounding the projectile on the bottom edge is FORBIDDEN.
- The projectile's bounding-box CENTER stays within ±5% of the canvas center on BOTH axes. It does NOT drift toward an edge, does NOT fly off-screen, does NOT shrink/grow toward an edge.
- The reference first frame already obeys this centered framing — preserve that composition exactly. Do NOT recompose closer to any edge, do NOT zoom in, do NOT crop.
- For ANY motion (spin, pulse, flicker, slow hover): the FARTHEST point of the body / tail MUST still be ≥8% inside ALL FOUR edges. If a planned motion would exceed that margin, REDUCE the motion amplitude until it fits.

CRITICAL FRAMING RULES (HARD CONSTRAINTS — never violate):
- HARD — CAMERA FROZEN (highest priority framing rule): This is a locked-off static camera. The projectile's on-screen bounding-box SIZE MUST stay within ±3% of the first frame at EVERY frame — never re-draw the body bigger or smaller on screen. The empty background margin on ALL FOUR edges MUST stay identical to the first frame; if any margin shrinks at any frame, the camera has zoomed in — which is FORBIDDEN. Treat the first frame's framing as a fixed, unchangeable crop: same on-screen size, same margins, same composition for the entire video.
- The projectile MUST NEVER exit the video frame. No part of the body or its attached tail may cross any of the four edges of the canvas at any frame.
- The projectile MUST NOT rest on or touch the bottom edge — it stays fully centered with margin below it too.
- Do NOT zoom, pan, or recompose. Keep the SAME framing, body size, and canvas composition as the reference first frame for every single frame of the video.
{effects_block}
STRICTLY AVOID (these issues were observed in failed attempts and must NOT recur):
- The projectile body drifting toward or resting on any edge (including the bottom edge — it is NOT grounded).
- Camera zoom-in, zoom-out, pan, dolly, or any change in framing across frames.
- The projectile growing or shrinking on screen (bounding-box size changing more than ±3% from the first frame) — this reads as a camera zoom and is forbidden.
- The background margin on ANY edge shrinking relative to the first frame (a tell-tale sign of an unwanted zoom-in).
- Any frame where the projectile (or its tail) is partially or fully off-screen or clipped by any edge.
- Spawning a second projectile, a muzzle flash, an impact burst, or any separate emitted particle effect around the body.
- Redrawing the projectile as a character, a creature with limbs, or a figure standing on the ground.
"""


# The ONLY two safe background colors allowed for i2v matting. Both
# are far from natural skin / cloth / metal / wood tones, both have
# zero overlap with the other, and both pair with deterministic
# chroma-key + RMBG-2.0 pipelines downstream.
SAFE_BG_OPTIONS: tuple[str, ...] = ("#FF00FF", "#00F000")


# Two tested presets, one per safe color. Effect palettes are picked
# to be ≥100 RGB-distance from the bg color so chroma-key and color-aug
# rescue don't eat them.
I2V_PROMPT_PRESETS: dict[str, dict[str, Any]] = {
    "magenta_bg": {
        # bg = #FF00FF (pure magenta) — pick this when the foreground has
        # NO magenta / pink / hot-pink pixels (most characters fit here).
        "bg_hex": "#FF00FF",
        "effect_palette": ["#00CED1", "#1E90FF", "#4169E1", "#80DEEA", "#FFFFFF"],
    },
    "green_bg": {
        # bg = #00F000 (chroma green) — pick this when the foreground has
        # magenta / pink / hot-pink pixels (which conflict with the magenta bg),
        # so the FARTHER safe color is green. Do NOT pick green for a GREEN
        # foreground (foliage / slime / grass armor) — a green subject on a
        # green key gets matted out; use magenta for those (PR #123 review #9).
        "bg_hex": "#00F000",
        "effect_palette": ["#FF00FF", "#1E90FF", "#FF6B00", "#FFD700", "#FFFFFF"],
    },
}


def detect_existing_safe_bg(
    image_path: str | Path,
    *,
    transparent_area_ratio: float = 0.02,
    border_coverage_ratio: float = 0.5,
    border_color_dist: float = 80.0,
) -> str | None:
    """Return the safe color that actually fills an opaque image border.

    Transparent images and opaque non-chroma images return ``None``. This
    function never invents a key color; callers may use :func:`pick_safe_bg`
    separately when they intend to stage a transparent image onto a new key.
    """
    import numpy as np
    from PIL import Image

    img = np.array(Image.open(str(image_path)).convert("RGBA"))
    h, w = img.shape[:2]
    alpha = img[:, :, 3]
    total_px = int(h * w)
    transparent_px = int((alpha <= 230).sum())
    has_real_alpha = (
        total_px > 0
        and (transparent_px / total_px) >= transparent_area_ratio
    )
    if has_real_alpha:
        return None

    rgb = img[:, :, :3].astype(np.float32)
    border_pixels = np.concatenate(
        [
            rgb[0, :, :].reshape(-1, 3),
            rgb[h - 1, :, :].reshape(-1, 3),
            rgb[:, 0, :].reshape(-1, 3),
            rgb[:, w - 1, :].reshape(-1, 3),
        ]
    )
    magenta = np.array([0xFF, 0x00, 0xFF], dtype=np.float32)
    green = np.array([0x00, 0xF0, 0x00], dtype=np.float32)
    n_border = max(1, len(border_pixels))
    cov_m = float(
        (np.sqrt(((border_pixels - magenta) ** 2).sum(axis=1)) <= border_color_dist).sum()
    ) / n_border
    cov_g = float(
        (np.sqrt(((border_pixels - green) ** 2).sum(axis=1)) <= border_color_dist).sum()
    ) / n_border
    if max(cov_m, cov_g) < border_coverage_ratio:
        return None
    return "#FF00FF" if cov_m >= cov_g else "#00F000"


def pick_safe_bg(
    image_path: str | Path,
    *,
    transparent_area_ratio: float = 0.02,
    border_coverage_ratio: float = 0.5,
    border_color_dist: float = 80.0,
) -> str:
    """Choose the safest chroma-key color for this first frame.

    If an opaque safe-color border already exists, that exact color is
    returned. Otherwise this function chooses the color furthest from the
    visible foreground. Choosing does NOT alter the image; callers handling a
    transparent frame must composite it with :func:`stage_transparent_on_safe_bg`
    before treating the result as the image's actual background.

    Opaque non-chroma images receive a color choice for diagnostics only; they
    are not valid Seedance overrides until explicitly converted to a keyed
    staging image.
    """
    import numpy as np
    from PIL import Image

    pil = Image.open(str(image_path)).convert("RGBA")
    img = np.array(pil)
    h, w = img.shape[:2]
    alpha = img[:, :, 3]
    rgb = img[:, :, :3].astype(np.float32)

    total_px = int(h * w)
    transparent_px = int((alpha <= 230).sum())
    # Require a meaningful transparent AREA — not one stray sub-threshold pixel.
    has_real_alpha = total_px > 0 and (transparent_px / total_px) >= transparent_area_ratio

    magenta = np.array([0xFF, 0x00, 0xFF], dtype=np.float32)
    green = np.array([0x00, 0xF0, 0x00], dtype=np.float32)

    detected = detect_existing_safe_bg(
        image_path,
        transparent_area_ratio=transparent_area_ratio,
        border_coverage_ratio=border_coverage_ratio,
        border_color_dist=border_color_dist,
    )
    if detected is not None:
        return detected

    if not has_real_alpha:
        fg = rgb.reshape(-1, 3)
    else:
        mask = alpha > 230
        fg = rgb[mask] if mask.any() else rgb.reshape(-1, 3)

    if len(fg) == 0:
        return "#FF00FF"

    dist_m = float(np.sqrt(((fg - magenta) ** 2).sum(axis=1)).min())
    dist_g = float(np.sqrt(((fg - green) ** 2).sum(axis=1)).min())

    # Pick the one that's further from any foreground pixel.
    return "#FF00FF" if dist_m >= dist_g else "#00F000"


def stage_transparent_on_safe_bg(
    image_path: str | Path,
    output_path: str | Path,
    *,
    bg_hex: str | None = None,
    transparent_area_ratio: float = 0.02,
) -> str:
    """Composite a genuinely transparent image onto a chosen safe color.

    Returns the color used. Opaque images are rejected because silently
    relabelling a white/JPEG background as magenta or green corrupts prompt and
    matting contracts.
    """
    from PIL import Image

    source = Image.open(str(image_path)).convert("RGBA")
    alpha = source.getchannel("A")
    total_px = max(1, source.width * source.height)
    transparent_px = sum(alpha.histogram()[:231])
    if transparent_px / total_px < transparent_area_ratio:
        raise ToolError(
            "INVALID_CONFIG",
            "first_frame_override is opaque and has no detectable safe-color "
            "background. Provide a validated #FF00FF/#00F000 frame or a "
            "transparent PNG that can be staged safely.",
        )
    chosen = bg_hex or pick_safe_bg(
        image_path, transparent_area_ratio=transparent_area_ratio
    )
    if chosen not in SAFE_BG_OPTIONS:
        raise ToolError("INVALID_CONFIG", f"Unsupported safe background: {chosen}")
    color = (
        (255, 0, 255, 255)
        if chosen == "#FF00FF"
        else (0, 240, 0, 255)
    )
    staged = Image.alpha_composite(Image.new("RGBA", source.size, color), source)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged.convert("RGB").save(destination, format="PNG")
    return chosen


def build_i2v_prompt(
    user_content: str,
    *,
    preset: str | None = None,
    bg_hex: str = "#FF00FF",
    effect_palette: list[str] | None = None,
    foreground_recolor_instructions: str = "",
    reference_image: str | Path | None = None,
    effects: bool = False,
    animation_profile: str = "character",
) -> str:
    """Wrap ``user_content`` with the iter_05-validated constraint block.

    ``bg_hex`` MUST be one of :data:`SAFE_BG_OPTIONS` (``#FF00FF`` or
    ``#00F000``). Pass ``reference_image`` only when it already has one
    of those keys; the function detects that actual border color and rejects
    transparent/non-chroma inputs because prompt construction cannot stage them.

    ``preset`` (``magenta_bg`` / ``green_bg``) overrides both
    ``bg_hex`` and ``effect_palette`` with the tested combination.

    ``effects`` defaults False because game FX are runtime ParticleEmitter
    recipes. True is reserved for an explicitly non-runtime/custom video.

    ``animation_profile`` selects the FRAMING contract of the generated
    prompt — it MUST agree with the validator rubric group the caller
    uses downstream, or generation and validation contradict each other
    (the projectile bug: the character template told the model to stand
    the subject on the bottom edge with anchored feet, while the
    projectile validator required a free-flying object that must NOT
    touch the bottom edge):

      - ``"character"`` (default) → grounded figure: feet on the bottom
        edge, body-only motion, character-language constraints.
      - ``"projectile"`` → free-flying projectile body: CENTERED with
        margin on all four edges, no feet / no ground contact, no
        character language. The no-effects block (when ``effects=False``)
        forbids only the separate runtime VFX, never "no projectiles".
    """
    if animation_profile not in ("character", "projectile"):
        raise ToolError(
            "INVALID_CONFIG",
            f"animation_profile must be 'character' or 'projectile', got "
            f"{animation_profile!r}",
        )
    is_projectile = animation_profile == "projectile"

    # 1) preset wins if explicit
    if preset is not None:
        if preset not in I2V_PROMPT_PRESETS:
            raise ToolError(
                "INVALID_CONFIG",
                f"Unknown i2v prompt preset: {preset!r}",
                hint=f"One of: {', '.join(I2V_PROMPT_PRESETS)}",
            )
        bg_hex = I2V_PROMPT_PRESETS[preset]["bg_hex"]
        effect_palette = list(I2V_PROMPT_PRESETS[preset]["effect_palette"])
    # 2) A supplied reference must agree with the selected/preset key. Prompt
    # construction cannot stage or modify the image.
    if reference_image is not None:
        detected_bg = detect_existing_safe_bg(reference_image)
        if detected_bg is None:
            raise ToolError(
                "INVALID_CONFIG",
                "reference_image has no detectable #FF00FF/#00F000 background. "
                "Stage a transparent image first or use the composite sprite "
                "production tool.",
            )
        if preset is not None and detected_bg != bg_hex:
            raise ToolError(
                "INVALID_CONFIG",
                f"preset {preset!r} selects {bg_hex}, but reference_image "
                f"actually uses {detected_bg}.",
            )
        if preset is None:
            bg_hex = detected_bg

    # 3) hard validation: only the two safe colors are allowed
    if bg_hex not in SAFE_BG_OPTIONS:
        raise ToolError(
            "INVALID_CONFIG",
            f"bg_hex must be one of {SAFE_BG_OPTIONS}, got {bg_hex!r}",
            hint=(
                "Only #FF00FF (magenta) and #00F000 (chroma green) are "
                "matting-safe. Pass `preset='magenta_bg'` / 'green_bg', "
                "or `reference_image=...` to auto-pick the safer one."
            ),
        )

    # Default effect palette: pick the one matching bg_hex
    if effect_palette is None:
        for cfg in I2V_PROMPT_PRESETS.values():
            if cfg["bg_hex"] == bg_hex:
                effect_palette = list(cfg["effect_palette"])
                break
    palette_str = ", ".join(effect_palette or [])

    if foreground_recolor_instructions.strip():
        recolor_block = (
            "- The character has parts that conflict with the background color — "
            "recolor those specific parts as follows: "
            + foreground_recolor_instructions.strip()
            + " (keep the rest of the character's appearance unchanged).\n"
        )
    else:
        recolor_block = ""

    if effects:
        effects_block = _EFFECTS_BLOCK.format(
            palette_str=palette_str, bg_hex=bg_hex
        ).strip()
    elif is_projectile:
        # Projectile no-FX block forbids only SEPARATE runtime VFX — never
        # "no projectiles" (that would forbid the subject itself).
        effects_block = _PROJECTILE_NO_EFFECTS_BLOCK.strip()
    else:
        effects_block = _NO_EFFECTS_BLOCK.strip()

    template = (
        PROJECTILE_CONSTRAINT_TEMPLATE if is_projectile
        else CANONICAL_CONSTRAINT_TEMPLATE
    )
    return template.format(
        user_content=user_content.strip(),
        bg_hex=bg_hex,
        recolor_block=recolor_block,
        effects_block=effects_block,
    )


# -------------------- seedance video generation --------------------


def _image_url_content(url: str, role: str | None = None) -> dict[str, Any]:
    """Build a native doubao ``image_url`` content item.

    The native /api/v3 task endpoint nests the url under an
    ``image_url`` object (``{"type":"image_url","image_url":{"url":…}}``)
    and uses an optional top-level ``role`` (``first_frame`` /
    ``last_frame``).
    """
    item: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
    if role:
        item["role"] = role
    return item


def _bool_flag(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_prompt_flags(
    prompt: str,
    *,
    duration: int | str,
    ratio: str,
    resolution: str,
    seed: int | str | None,
    camerafixed: bool | None,
    watermark: bool | None,
    extra_flags: dict[str, Any] | None,
) -> str:
    """Append doubao text-command flags to ``prompt``.

    The native endpoint reads generation params from trailing flags in
    the text content (``--rs`` resolution, ``--rt`` ratio, ``--dur``
    duration, ``--seed``, ``--cf`` camerafixed, ``--wm`` watermark)
    rather than from a separate ``params`` object.
    """
    flags: list[str] = [
        f"--rs {resolution}",
        f"--rt {ratio}",
        f"--dur {duration}",
    ]
    if seed is not None:
        flags.append(f"--seed {seed}")
    if camerafixed is not None:
        flags.append(f"--cf {_bool_flag(camerafixed)}")
    if watermark is not None:
        flags.append(f"--wm {_bool_flag(watermark)}")
    if extra_flags:
        for key, val in extra_flags.items():
            flag = key if key.startswith("--") else f"--{key}"
            flags.append(f"{flag} {val}")
    return f"{prompt.strip()} {' '.join(flags)}"


def _poll_task(
    config: Config,
    *,
    provider: Any,
    endpoint: str,
    verify_ssl: bool,
    task_id: str,
    native_model: str,
    server_timeout: int,
) -> dict[str, Any]:
    """Poll ``{endpoint}/{task_id}`` until the task reaches a terminal state.

    ARK auth headers are rebuilt for each poll; transient network
    errors are retried within the overall deadline.
    """
    poll_url = f"{endpoint}/{task_id}"
    interval = config.seedance.poll_interval_seconds
    started = time.monotonic()
    deadline = started + server_timeout
    last_status = "?"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        request_id = f"seedance-poll-{uuid.uuid4().hex[:12]}"
        headers = provider.video_auth_headers(
            config,
            request_id=request_id,
            model=native_model,
            timeout_s=max(1, min(60, int(remaining))),
        )
        try:
            resp = requests.get(
                poll_url,
                headers=headers,
                timeout=min(60.0, remaining),
                verify=verify_ssl,
            )
        except requests.exceptions.RequestException:
            sleep_for = min(interval, max(0.0, deadline - time.monotonic()))
            if sleep_for:
                time.sleep(sleep_for)
            continue

        if resp.status_code in (401, 403):
            raise ToolError(
                "AUTH_FAILED",
                f"seedance poll authentication failed for task {task_id}.",
                hint=resp.text[:300],
            )
        if resp.status_code >= 400:
            retryable = (
                resp.status_code in {408, 409, 425, 429}
                or resp.status_code >= 500
            )
            if not retryable:
                raise ToolError(
                    "SEEDANCE_FAILED",
                    f"seedance poll returned HTTP {resp.status_code} "
                    f"for task {task_id}.",
                    hint=resp.text[:300],
                )
            sleep_for = min(interval, max(0.0, deadline - time.monotonic()))
            if sleep_for:
                time.sleep(sleep_for)
            continue
        try:
            data = resp.json()
        except ValueError as exc:
            raise ToolError(
                "BAD_RESPONSE",
                f"seedance poll returned non-JSON HTTP {resp.status_code}.",
                hint=resp.text[:300],
            ) from exc
        if not isinstance(data, dict):
            raise ToolError(
                "BAD_RESPONSE",
                "seedance poll response was not an object.",
            )
        if data.get("error"):
            raise ToolError(
                "SEEDANCE_FAILED",
                f"seedance task {task_id} poll failed: {str(data['error'])[:500]}",
            )
        last_status = str(data.get("status", "")) or "?"
        if last_status == "succeeded":
            return data
        if last_status in ("failed", "cancelled"):
            err = data.get("error") or data.get("msg") or data
            raise ToolError(
                "SEEDANCE_FAILED",
                f"seedance task {task_id} {last_status}: {err}",
            )
        sleep_for = min(interval, max(0.0, deadline - time.monotonic()))
        if sleep_for:
            time.sleep(sleep_for)

    elapsed = round(time.monotonic() - started, 1)
    raise ToolError(
        "SEEDANCE_TIMEOUT",
        f"seedance task {task_id} did not finish within {server_timeout}s "
        f"(elapsed: {elapsed}s; last status: {last_status}).",
    )


def _request_native_video(
    config: Config,
    *,
    provider: Any,
    prompt: str,
    model_id: str,
    duration: int | str,
    ratio: str,
    resolution: str,
    seed: int | str | None,
    camerafixed: bool | None,
    watermark: bool | None,
    image_urls: list[str],
    first_frame_url: str | None,
    last_frame_url: str | None,
    extra_params: dict[str, Any] | None,
    server_timeout: int,
) -> dict[str, Any]:
    """Run the public ARK native submit/poll protocol."""
    endpoint = provider.video_endpoint(config)
    verify_ssl = provider.video_verify_ssl(config)
    flagged_prompt = _build_prompt_flags(
        prompt,
        duration=duration,
        ratio=ratio,
        resolution=resolution,
        seed=seed,
        camerafixed=camerafixed,
        watermark=watermark,
        extra_flags=extra_params,
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": flagged_prompt}]
    content.extend(_image_url_content(url) for url in image_urls)
    if first_frame_url:
        content.append(_image_url_content(first_frame_url, "first_frame"))
    if last_frame_url:
        content.append(_image_url_content(last_frame_url, "last_frame"))

    request_id = f"seedance-submit-{uuid.uuid4().hex[:12]}"
    submit_headers = provider.video_auth_headers(
        config,
        request_id=request_id,
        model=model_id,
        timeout_s=min(120, server_timeout),
    )
    try:
        response = requests.post(
            endpoint,
            headers=submit_headers,
            json={"model": model_id, "content": content},
            timeout=min(180, server_timeout),
            verify=verify_ssl,
        )
    except requests.exceptions.RequestException as exc:
        raise ToolError(
            "NETWORK_ERROR",
            f"seedance submit failed: {exc}",
        ) from exc

    try:
        submitted = response.json()
    except ValueError as exc:
        raise ToolError(
            "BAD_RESPONSE",
            f"seedance non-JSON submit response ({response.status_code}): "
            f"{response.text[:500]}",
        ) from exc
    if response.status_code in (401, 403):
        raise ToolError(
            "AUTH_FAILED",
            f"seedance auth failed: {str(submitted)[:500]}",
        )
    if response.status_code >= 400:
        raise ToolError(
            "SEEDANCE_FAILED",
            f"seedance submit returned HTTP {response.status_code}: "
            f"{str(submitted)[:500]}",
        )
    if not isinstance(submitted, dict):
        raise ToolError(
            "BAD_RESPONSE",
            "seedance submit response was not an object.",
        )
    if submitted.get("error"):
        raise ToolError(
            "SEEDANCE_FAILED",
            f"seedance submit error: {submitted['error']}",
        )
    task_id = submitted.get("id")
    if not task_id:
        raise ToolError(
            "SEEDANCE_NO_OUTPUT",
            f"seedance submit returned no task id: {submitted}",
        )

    final = _poll_task(
        config,
        provider=provider,
        endpoint=endpoint,
        verify_ssl=verify_ssl,
        task_id=str(task_id),
        native_model=model_id,
        server_timeout=server_timeout,
    )
    task_content = final.get("content") or {}
    video_url = (
        task_content.get("video_url")
        if isinstance(task_content, dict)
        else None
    )
    if not video_url:
        raise ToolError(
            "SEEDANCE_NO_OUTPUT",
            f"seedance task {task_id} succeeded but returned no video_url.",
        )
    return {
        "video_url": str(video_url),
        "request_id": request_id,
        "task_id": str(task_id),
        "model": model_id,
        "model_id": model_id,
        "params": {
            "duration": str(duration),
            "ratio": final.get("ratio", ratio),
            "resolution": final.get("resolution", resolution),
            "seed": final.get("seed", seed),
        },
        "protocol": "ark_native_task",
        "raw_response": final,
    }


def generate_video(
    config: Config,
    *,
    prompt: str,
    output: Path,
    model: str = "fast",
    model_id: str | None = None,
    duration: int | str = 4,
    ratio: str = "16:9",
    resolution: str = "480p",
    seed: int | str | None = None,
    camerafixed: bool | None = None,
    watermark: bool | None = None,
    image: list[str] | None = None,
    first_frame: str | None = None,
    last_frame: str | None = None,
    extra_params: dict[str, Any] | None = None,
    save_response: Path | None = None,
    server_timeout: int = 600,
    download_timeout: int = 120,
    print_url_only: bool = False,
) -> dict[str, Any]:
    """Generate through ARK's asynchronous task protocol and optionally download the MP4."""
    provider = get_provider(config)
    resolved_model = provider.resolve_video_model(
        config,
        model=model,
        model_id=model_id,
    )
    image_urls = [
        provider.ensure_image_url(config, v) for v in (image or [])
    ]
    first_frame_url = (
        provider.ensure_image_url(config, first_frame) if first_frame else None
    )
    last_frame_url = (
        provider.ensure_image_url(config, last_frame) if last_frame else None
    )
    runtime_defaults = getattr(getattr(config, "assets", None), "seedance", None)
    transient_max_attempts = int(
        getattr(
            runtime_defaults,
            "transient_max_attempts",
            AssetSeedanceDefaults.transient_max_attempts,
        )
    )
    transient_retry_delay = float(
        getattr(
            runtime_defaults,
            "transient_retry_delay_seconds",
            AssetSeedanceDefaults.transient_retry_delay_seconds,
        )
    )

    result: dict[str, Any] | None = None
    generation_attempts = 0
    while generation_attempts < transient_max_attempts:
        generation_attempts += 1
        try:
            candidate_result = provider.request_video(
                config,
                prompt=prompt,
                model_id=resolved_model,
                duration=duration,
                ratio=ratio,
                resolution=resolution,
                seed=seed,
                camerafixed=camerafixed,
                watermark=watermark,
                image_urls=image_urls,
                first_frame_url=first_frame_url,
                last_frame_url=last_frame_url,
                extra_params=extra_params,
                server_timeout=server_timeout,
            )
            if not isinstance(candidate_result, dict):
                raise ToolError(
                    "BAD_RESPONSE",
                    "The Seedance provider returned a non-object response.",
                )
            if not candidate_result.get("video_url"):
                raise ToolError(
                    "SEEDANCE_NO_OUTPUT",
                    "The Seedance provider returned no video_url.",
                )
            result = candidate_result
            break
        except ToolError as exc:
            if (
                generation_attempts >= transient_max_attempts
                or not is_transient_seedance_error(exc)
            ):
                raise
            if transient_retry_delay > 0:
                time.sleep(transient_retry_delay)

    if result is None:  # Defensive: the bounded loop either returns or raises.
        raise ToolError("SEEDANCE_NO_OUTPUT", "Seedance returned no usable video.")

    raw_response = result.pop("raw_response", result)
    if save_response is not None:
        save_response.parent.mkdir(parents=True, exist_ok=True)
        save_response.write_text(
            json.dumps(raw_response, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    video_url = str(result["video_url"])
    result["generation_attempts"] = generation_attempts
    if not print_url_only:
        verify_ssl = provider.video_verify_ssl(config)
        output.parent.mkdir(parents=True, exist_ok=True)
        partial_output = output.with_name(f".{output.name}.part")
        download_attempts = 0
        while download_attempts < transient_max_attempts:
            download_attempts += 1
            partial_output.unlink(missing_ok=True)
            try:
                with requests.get(
                    video_url,
                    stream=True,
                    timeout=download_timeout,
                    verify=verify_ssl,
                ) as r:
                    r.raise_for_status()
                    with partial_output.open("wb") as fh:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                fh.write(chunk)
                partial_output.replace(output)
                break
            except (requests.exceptions.RequestException, OSError) as exc:
                partial_output.unlink(missing_ok=True)
                if download_attempts >= transient_max_attempts:
                    raise ToolError(
                        "NETWORK_ERROR",
                        f"failed to download seedance video: {exc}",
                    ) from exc
                if transient_retry_delay > 0:
                    time.sleep(transient_retry_delay)
        result["output"] = str(output)
        result["download_attempts"] = download_attempts

    return result


__all__ = [
    "CANONICAL_CONSTRAINT_TEMPLATE",
    "PROJECTILE_CONSTRAINT_TEMPLATE",
    "I2V_PROMPT_PRESETS",
    "SAFE_BG_OPTIONS",
    "build_i2v_prompt",
    "detect_existing_safe_bg",
    "generate_video",
    "pick_safe_bg",
    "stage_transparent_on_safe_bg",
]
