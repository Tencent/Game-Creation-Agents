"""GPT-Image-2 adapter — text-to-image and image edit via OpenAI.

Replaces the standalone ``generate_image.py`` script. The HTTP shape and
retry logic are preserved 1:1 so existing skills keep producing the
same artefacts.

Image-generation prompts are wrapped with a hard color constraint (no
pure magenta / no pure chroma green anywhere in the asset) by default,
so downstream chroma-key + RMBG-2.0 matting can't accidentally eat the
foreground. Pass ``enforce_safe_bg_avoidance=False`` to opt out (rare —
only when the asset really needs those colors).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ToolError
from ..providers import get_provider

# The two reserved chroma-key bg colors. Any generated asset (foreground
# / character / props / icons) MUST stay far from both, otherwise the
# matting step downstream will eat parts of the asset.
SAFE_BG_COLORS: tuple[str, ...] = ("#FF00FF", "#00F000")

# Asset types whose output fills the canvas edge to edge. They take the
# ``full_scene`` framing profile rather than a centered subject with margins.
FULL_SCENE_ASSET_TYPES: frozenset[str] = frozenset({"background", "map", "parallax"})

# Full-scene types that are ALSO opaque by default: not matted, and given no
# chroma-key plate, so they keep whatever the model paints. Every other asset
# type (prop, tile/tileset, ui, icon, sprite, …) is matted, so when the caller
# omits ``safe_bg_color`` we fill the canvas with the reserved chroma-key color
# instead of letting the model pick an unremovable background.
#
# ``parallax`` is deliberately absent. A parallax layer is an overlay composited
# above the sky, so it needs real alpha over its content line; only the
# bottom-most layer is genuinely opaque, and that one says so per call with
# ``skip_matting``. Treating the whole type as opaque left a transparent
# parallax band unreachable: the producer forced the magenta plate to stay, and
# any other asset_type broke the manifest's assetType parity check.
OPAQUE_SCENE_ASSET_TYPES: frozenset[str] = frozenset({"background", "map"})

# Asset types that are GROUNDED CHARACTER first frames — a full-body figure that
# must stand on the bottom edge so downstream Seedance i2v keeps it grounded.
# ONLY these get the foot-anchor framing block. A projectile *body* is NOT here
# (it has no feet / ground contact) — it is a centered_asset by default.
GROUNDED_SPRITE_ASSET_TYPES: frozenset[str] = frozenset(
    {"sprite", "character", "player", "npc", "creature", "enemy", "boss", "reference"}
)

# The three mutually-exclusive framing profiles. Exactly one may enter a prompt.
FRAMING_PROFILES: tuple[str, ...] = ("grounded_sprite", "centered_asset", "full_scene")


def default_safe_bg_color(
    asset_type: str | None,
    configured: str = "#FF00FF",
    *,
    default_when_unknown: bool = False,
) -> str | None:
    """Resolve the canvas-fill safe bg when a caller omitted ``safe_bg_color``.

    - Opaque scene types (``background``/``map``/``parallax``) → ``None`` (no
      fill; the model paints the full scene).
    - Any other *explicit* ``asset_type`` (prop/tile/ui/icon/sprite/…) →
      ``configured`` so the background is removable downstream.
    - Unknown / ``None`` ``asset_type`` → controlled by ``default_when_unknown``:
      ``generate_image`` keeps it ``None`` (a background may legitimately omit
      asset_type, and the matting gate is the backstop), while ``edit_image``
      passes ``True`` — an edit with no asset_type is almost always lettering /
      element extraction that MUST be matted, so it gets the safe color; an
      opaque scene edit (e.g. map-base seeding) must declare ``asset_type``.
    """
    at = (asset_type or "").strip().lower()
    if at in OPAQUE_SCENE_ASSET_TYPES:
        return None
    if not at:
        return configured if default_when_unknown else None
    return configured


SAFE_BG_AVOID_BLOCK = """\
CRITICAL COLOR CONSTRAINT (HARD — never violate):
- The generated foreground MUST contain no pixels close to the selected matting key {safe_bg_color}; keep RGB distance ≥100 from that key.
- {safe_bg_color} is reserved exclusively for the edge-to-edge background. Any foreground pixel close to it may be removed during matting.
- The alternate safe color is NOT forbidden: preserve requested pink/magenta foreground when the key is green, and requested green foreground when the key is magenta.
"""


CHARACTER_FOOT_ANCHOR_BLOCK = """\
CRITICAL COMPOSITION REQUIREMENTS (HARD — never violate):
- The character's feet (or lowest body part — paws, hooves, base, wheels, tail-tip if quadrupedal) MUST touch the bottom edge of the canvas. A small margin of 0–4 pixels between the lowest pixel and the absolute bottom edge is acceptable.
- The character is horizontally centered on the canvas (the character's mid-line aligns with the canvas mid-line).
- Leave generous empty solid-color space ABOVE the character — at least 20-30% of the canvas height — so downstream i2v can swing weapons / cast spells / jump upward without clipping the top edge.
- Leave generous empty solid-color space on BOTH the LEFT and RIGHT sides of the character — at least 15-20% of canvas width on EACH side. This margin is for downstream i2v swing arcs (sword backswing windup, staff sweep, kick extension, dash motion, projectile trails). If any held weapon, prop, or limb is already near a left or right edge in this static frame, REDUCE the character's size or scoot it inward until ALL four sides have ≥15% empty bg margin.
- Combined budget: the character + ALL its held items / hair / cape / props occupy the CENTER region of the canvas (roughly the middle 60-70% horizontally, lower 60-75% vertically). Top 20-30%, left 15-20%, right 15-20% are pure empty bg — these are i2v safety buffers.
- Do NOT center the character vertically. Do NOT leave large empty space below the feet.
- Single full-body character only — no extra figures, no second character, no thumbnails, no border, no UI, no caption, no text.
"""


# Subject-centered margin constraint applied to EVERY generated image
# (whether or not safe_bg_color is set). The foot-anchor block above is
# specific to characters that need to stand on a chroma-key bg; this
# block is the universal "don't fill the entire canvas, leave margin so
# the subject isn't clipped at the edges" rule. Used by qwen-image-2
# and any other generator that tends to compose subjects flush against
# the canvas edges.
SUBJECT_MARGIN_BLOCK = """\
CRITICAL FRAMING — SUBJECT IS CENTERED WITH MARGIN (HARD CONSTRAINT, never violate):
- The PRIMARY SUBJECT (character / item / prop / icon / UI piece) MUST be horizontally and vertically centered in the canvas. The subject's bounding box center aligns with the canvas center within ±5% on each axis.
- The subject's bounding box MUST leave AT LEAST 8% empty margin on each of the four edges (top, bottom, left, right). For a 1024×1024 canvas this is ≥82 pixels of clear background on every side.
- The subject's bounding box MUST occupy AT MOST 85% of the canvas's shorter side. Do NOT let the subject (or any held weapon, prop, hair, cape, extending limb, glow halo) touch or cross any of the four edges.
- The remaining margin space is FLAT BACKGROUND — uniform color, no scene, no horizon line, no shadow on background, no halo bleed.
- If a held weapon or extending limb would otherwise cross an edge at the requested subject size, REDUCE the subject's size until ALL of it (including the weapon/limb/halo) fits within the inner 84% × 84% region with margin on every side.
- Single subject only — no second figure, thumbnails, border frame, UI overlay, or unrelated caption. If the requested subject IS stylized lettering/logo art, preserve that requested text exactly and add no extra text.
"""


# Flat-2D-VIEW constraint for full-SCENE concept mockups (and any other
# generation that must read as a flat 2D game the Phaser runtime can reproduce).
# gpt-image-2 has a strong 3D/photographic prior for physical objects (dominoes,
# dice, blocks, balls, bells, ramps), so a casual trailing "no 3D" line in the
# user prompt is routinely ignored — the same reason the foot-anchor /
# subject-margin blocks exist. This block is injected at the FRONT of the prompt
# as a hard constraint.
#
# IMPORTANT — what this locks is the VIEW/PROJECTION, not the shading/finish.
# The runtime needs a flat camera (straight side-on or top-down) so the layout
# and per-object crops are reproducible; it does NOT need the art to be
# flat-colored. Depth may be PAINTED (shading, volume, materials) but never
# PROJECTED (perspective, isometric, extruded 3D bodies). Forcing flat fills
# here is what made downstream assets look unrefined, so the block now permits
# rich rendering within the flat silhouette.
FLAT_2D_SCENE_BLOCK = """\
CRITICAL VIEW — FLAT 2D GAME VIEW (HARD CONSTRAINT, never violate):
- Compose the image as a 2D Phaser sprite game / flat mobile-puzzle UI screen seen through ONE flat camera: either STRAIGHT TOP-DOWN (bird's-eye, camera pointing straight down) OR STRAIGHT SIDE-ON (orthographic side elevation). No camera tilt. This is NOT a 3D-rendered scene, NOT a photo, NOT a CGI diorama.
- FORBIDDEN (these break the flat 2D view the engine must reproduce): isometric or axonometric projection; 3D perspective, vanishing points, or converging lines; a tilted/angled camera; a floor/table/ground plane that recedes into the distance; a horizon line; scene-wide foreshortening; drawing an object as an extruded 3D body / perspective box with visible receding side or top faces; long cast or drop shadows that imply height off the ground.
- Depth may be PAINTED, never PROJECTED. Each object keeps its flat top outline (top-down) or flat face outline (side-on), but WITHIN that silhouette it can be fully shaded and rendered — soft/gradient shading, volume, highlights, rim light, and rich materials in the chosen art style are welcome so assets look polished (go truly flat/cel-only when the art style is explicitly flat/vector). The constraint is the camera, not the finish.
- Even for things normally seen in 3D (dominoes, dice, blocks, ramps, balls, bells, coins, bottles): draw the flat-view version — its top-down or side-on silhouette — shaded however the art style calls for, but never a perspective/extruded 3D object.
"""


def default_framing_profile(asset_type: str | None) -> str:
    """Derive the framing profile from the asset's ROLE (``asset_type``).

    This is the SINGLE source of the framing default — it is decided by what the
    asset IS, never by whether a chroma-key ``safe_bg_color`` happens to be set
    (the old bug: ``produce_static_asset`` auto-fills a safe bg for every matted
    prop/UI/icon, which under the old rule handed them the grounded-*character*
    framing block — "single full-body character only", "no UI"). Mapping:

      - grounded character types (sprite/character/player/npc/creature/enemy/
        boss/reference) → ``grounded_sprite`` (feet on the bottom edge).
      - opaque scene types (background/map/parallax) → ``full_scene`` (no
        single-subject framing block at all).
      - EVERY other explicit type (prop/item/tile/tileset/ui/icon/button/hud/
        fx/projectile/impact/…) → ``centered_asset`` (a single subject centered
        with margin on every edge).
      - unknown / ``None`` → ``centered_asset`` (the safe default for a matted
        still asset; a full scene must declare an opaque ``asset_type`` or pass
        ``framing_profile='full_scene'`` explicitly).
    """
    at = (asset_type or "").strip().lower()
    if at in GROUNDED_SPRITE_ASSET_TYPES:
        return "grounded_sprite"
    if at in FULL_SCENE_ASSET_TYPES:
        return "full_scene"
    return "centered_asset"


def resolve_framing_profile(
    *,
    framing_profile: str | None = None,
    asset_type: str | None = None,
    subject_margin: bool = True,
    flat_2d: bool = False,
) -> str:
    """Resolve the ONE mutually-exclusive framing profile for a prompt.

    Exactly one framing block may ever enter a prompt — the foot-anchor block
    and the subject-margin block are CONTRADICTORY (feet glued to the bottom
    edge + no space below the feet vs. vertically centered + ≥8% bottom margin),
    so injecting both produces an unsatisfiable prompt. The returned profile is
    one of :data:`FRAMING_PROFILES`:

      - ``"grounded_sprite"`` — chroma-key character that must stand on the
        bottom edge for downstream i2v. Injects CHARACTER_FOOT_ANCHOR_BLOCK
        ONLY (that block already carries its own top/left/right margin rules).
      - ``"centered_asset"`` — a single prop / item / icon / UI piece centered
        with margin on every edge. Injects SUBJECT_MARGIN_BLOCK only.
      - ``"full_scene"`` — a full-canvas scene (map / parallax / mockup) that
        must NOT have any single-subject framing block. Injects neither.

    Precedence:
      1. an explicit ``framing_profile`` (validated against
         :data:`FRAMING_PROFILES`) always wins — this is how a caller corrects
         a mis-classified asset;
      2. else the profile is derived from ``asset_type`` via
         :func:`default_framing_profile` (role-based — NOT bg-color-based);
      3. ``subject_margin=False`` demotes a would-be ``centered_asset`` to
         ``full_scene`` (a caller opting a matted-type asset out of the
         single-subject block, e.g. a tileable texture) — it never promotes a
         grounded sprite or a full scene.

    ``flat_2d`` is orthogonal (it constrains the VIEW, not the subject framing)
    and is applied on top of any profile by the caller.
    """
    if framing_profile is not None:
        prof = framing_profile.strip().lower()
        if prof not in FRAMING_PROFILES:
            raise ToolError(
                "INVALID_CONFIG",
                f"framing_profile must be one of {FRAMING_PROFILES}, got {framing_profile!r}",
                hint=(
                    "grounded_sprite = grounded character first frame; "
                    "centered_asset = prop/UI/icon; full_scene = background/map/mockup."
                ),
            )
        return prof

    profile = default_framing_profile(asset_type)
    # subject_margin=False is a caller opt-OUT of the single-subject block for a
    # matted-type asset (tileable texture, full-bleed panel). It only demotes a
    # centered_asset → full_scene; it never overrides a grounded sprite.
    if profile == "centered_asset" and not subject_margin:
        return "full_scene"
    return profile


def wrap_prompt_with_safe_bg(
    user_prompt: str,
    *,
    safe_bg_color: str | None = None,
    framing_profile: str | None = None,
    asset_type: str | None = None,
    subject_margin: bool = True,
    flat_2d: bool = False,
) -> str:
    """Prepend the safe-bg avoidance block (and optional bg-color spec
    + exactly one framing block) to ``user_prompt``.

    ``safe_bg_color`` (when set, must be one of :data:`SAFE_BG_COLORS`)
    asks the model to fill the canvas with that solid color so the
    output is ready to feed into Seedance i2v + matting. It is ONLY a
    background-fill request — it no longer influences which framing
    block is chosen (that was the IMG-01 bug: ``produce_static_asset``
    auto-fills a safe bg for every matted prop/UI/icon, which under the
    old rule handed them the grounded-*character* block).

    The framing block is resolved to a SINGLE mutually-exclusive profile
    via :func:`resolve_framing_profile`, whose default is derived from the
    asset's ROLE (``asset_type``), not from ``safe_bg_color``:

      - ``framing_profile`` (explicit ``grounded_sprite`` / ``centered_asset``
        / ``full_scene``) always wins — the caller's escape hatch.
      - else the default comes from ``asset_type``: grounded character types →
        grounded_sprite; opaque scene types → full_scene; every other type
        (prop/ui/icon/tile/…) → centered_asset.
      - ``subject_margin=False`` demotes a centered_asset → full_scene (opt out
        of the single-subject block for a tileable texture / full-bleed panel).

    Role-less safety (PR #123 review #3): when BOTH ``framing_profile`` and
    ``asset_type`` are omitted, the default is ``centered_asset`` (a single
    subject with margin) — NEVER the grounded full-body-CHARACTER block. To
    produce a grounded character, pass ``framing_profile='grounded_sprite'`` or
    a grounded ``asset_type`` explicitly.

    ``flat_2d`` (default False) prepends FLAT_2D_SCENE_BLOCK at the FRONT so the
    image reads through a flat 2D game camera (no isometric / perspective /
    extruded-3D projection). It constrains the VIEW only.
    """
    if safe_bg_color is not None and safe_bg_color not in SAFE_BG_COLORS:
        raise ToolError(
            "INVALID_CONFIG",
            f"safe_bg_color must be one of {SAFE_BG_COLORS}, got {safe_bg_color!r}",
            hint="Only #FF00FF (magenta) or #00F000 (chroma green) are matting-safe.",
        )

    # Role-based, ALWAYS: no background-color fallback path. A role-less
    # call (no framing_profile, no asset_type) resolves to centered_asset via
    # default_framing_profile(None) — the safe single-subject default — and is
    # demoted to full_scene by subject_margin=False. It can NEVER become the
    # grounded-character block by accident (review #3).
    profile = resolve_framing_profile(
        framing_profile=framing_profile,
        asset_type=asset_type,
        subject_margin=subject_margin,
        flat_2d=flat_2d,
    )

    parts: list[str] = []
    if flat_2d:
        parts.append(FLAT_2D_SCENE_BLOCK)
    if safe_bg_color is not None:
        parts.append(
            f"Generate the character / asset on a solid pure {safe_bg_color} "
            f"background that fills the entire canvas edge-to-edge. "
            f"The background color must be flat, uniform, and exactly {safe_bg_color}."
        )
    # Exactly one framing block — never both (they are mutually exclusive).
    if profile == "grounded_sprite":
        parts.append(CHARACTER_FOOT_ANCHOR_BLOCK)
    elif profile == "centered_asset":
        parts.append(SUBJECT_MARGIN_BLOCK)
    parts.append(user_prompt.strip())
    if safe_bg_color is not None:
        parts.append(SAFE_BG_AVOID_BLOCK.format(safe_bg_color=safe_bg_color))
    return "\n\n".join(parts)


# -------------------- adaptive output sizing --------------------
#
# Default is size="auto": we pass "auto" STRAIGHT THROUGH to gpt-image-2
# and let the model choose the resolution from the prompt — genuine
# model-side adaptivity, no hardcoded number. The ladder below is an
# OPTIONAL override: pass `asset_type`/`aspect` only when you want to CAP
# resolution, because "auto" optimizes for the content, not for the
# smallest / cheapest legal size. An explicit "WxH" always wins.
#
# gpt-image accepts ARBITRARY W×H on some backends (unlike the fixed
# 1024²/1536×1024/1024×1536 set), but any EXPLICIT size must satisfy ALL of:
#   • both edges are multiples of 16px
#   • 655,360 <= total pixels <= 8,294,400   (≈816² floor, ≈4K ceiling)
#   • max edge <= 3840px
#   • long-edge : short-edge ratio <= 3:1
# So the smallest LEGAL square is 816×816 — you cannot generate a 512² or
# 768² icon; downscale AFTER generation for genuinely small runtime assets.
#
# Ladder rationale: sprites/characters feed Seedance i2v, whose 1:1 output
# tops out at 960×960 (720p) / 640×640 (480p), so 960² is the right-sized
# first frame (no i2v upscale). Backgrounds go 16:9. Props/UI gain no
# detail past the floor, so they use the 816² minimum.

GPT_IMAGE_2_MIN_PIXELS = 655_360
GPT_IMAGE_2_MAX_PIXELS = 8_294_400
GPT_IMAGE_2_MAX_EDGE = 3840
GPT_IMAGE_2_EDGE_STEP = 16

# ``normalize_size`` selection knobs (IMG-02). The objective is "the gpt-image-2
# grid point closest to a UNIFORM SCALE of the requested size at the requested
# AREA" — a pure resize, aspect and resolution preserved as far as the 16px grid
# and the legal band allow. NORMALIZE_ASPECT_LOG_TOL is the log-space aspect
# window used by the tolerance regression test; NORMALIZE_MAX_AREA_INFLATION is
# the documented ceiling a below-floor (mobile) canvas may be inflated to over
# the pixel floor (also a regression bound). The selection itself is now a LOCAL
# search around the area-correct ideal point, so neither ceiling drift (IMG-02
# original) nor the "cap relative to the smallest in-tolerance area" collapse
# (PR #123 review #4) can re-appear.
NORMALIZE_ASPECT_LOG_TOL = 0.05
NORMALIZE_MAX_AREA_INFLATION = 2.0

DEFAULT_SIZE = "1024x1024"


def is_valid_gpt_image_2_size(size: str) -> bool:
    """True if ``size`` (``"WxH"``) meets every gpt-image-2 constraint.

    Used to keep the size ladder honest (see the test suite) and as a
    cheap guard before sending a request.
    """
    try:
        w_str, _, h_str = size.lower().partition("x")
        w, h = int(w_str), int(h_str)
    except (ValueError, AttributeError):
        return False
    if w <= 0 or h <= 0:
        return False
    if w % GPT_IMAGE_2_EDGE_STEP or h % GPT_IMAGE_2_EDGE_STEP:
        return False
    if max(w, h) > GPT_IMAGE_2_MAX_EDGE:
        return False
    if max(w, h) / min(w, h) > 3.0:
        return False
    return GPT_IMAGE_2_MIN_PIXELS <= w * h <= GPT_IMAGE_2_MAX_PIXELS


def resolve_size(
    size: str | None,
    *,
    asset_type: str | None = None,
    aspect: str | None = None,
    size_by_asset_type: Mapping[str, str] | None = None,
    size_by_aspect: Mapping[str, str] | None = None,
) -> str:
    """Resolve the value sent to the API as ``size``.

    Precedence:
      1. an explicit ``size`` ("WxH") always wins;
      2. a *known* ``aspect`` → its configured tuned size;
      3. a *known* ``asset_type`` → its configured tuned size;
      4. otherwise ``"auto"`` — pass through to gpt-image-2 so the model
         picks the resolution itself (true adaptivity).

    Unknown ``asset_type`` / ``aspect`` hints fall through to ``"auto"``
    rather than a hardcoded default, so the model still adapts.
    """
    if size and size.strip().lower() != "auto":
        return size.strip()
    if aspect:
        aspect_key = aspect.strip().lower()
        hit = size_by_aspect.get(aspect_key) if size_by_aspect else None
        if not hit:
            return "auto"
        if not is_valid_gpt_image_2_size(hit):
            raise ToolError(
                "INVALID_CONFIG",
                f"assets.image.size_by_aspect.{aspect_key} is not a legal size: {hit!r}.",
            )
        return hit
    if asset_type:
        asset_key = asset_type.strip().lower()
        hit = size_by_asset_type.get(asset_key) if size_by_asset_type else None
        if hit:
            if not is_valid_gpt_image_2_size(hit):
                raise ToolError(
                    "INVALID_CONFIG",
                    f"assets.image.size_by_asset_type.{asset_key} is not a legal "
                    f"size: {hit!r}.",
                )
            return hit
    return "auto"


def normalize_size(size: str) -> str:
    """Map an arbitrary ``"WxH"`` to the nearest gpt-image-2-legal size (aspect kept).

    Unlike :func:`resolve_size` (which handles only the tuned ladder + ``"auto"``),
    this takes a concrete requested resolution — e.g. a game's real canvas like
    ``"960x540"`` or ``"375x667"`` — and returns the closest size satisfying every
    gpt-image-2 constraint, with the aspect ratio preserved as closely as the 16px
    grid allows. This is what lets a concept mockup be generated at the game's real
    aspect so a later ``fit_image_to_resolution`` is a pure resize (no crop).
    Constraints (identical to :func:`is_valid_gpt_image_2_size`):

      - both edges multiples of ``GPT_IMAGE_2_EDGE_STEP`` (16),
      - max edge ≤ ``GPT_IMAGE_2_MAX_EDGE`` (3840),
      - long:short ratio ≤ 3:1,
      - ``GPT_IMAGE_2_MIN_PIXELS`` ≤ total ≤ ``GPT_IMAGE_2_MAX_PIXELS``.

    The return value is GUARANTEED to satisfy :func:`is_valid_gpt_image_2_size`
    for any parseable positive input; unparseable / non-positive input falls back
    to :data:`DEFAULT_SIZE` (itself legal).

    Selection objective (IMG-02 + PR #123 review #4): the returned size is the
    gpt-image-2 grid point closest to a UNIFORM SCALE of the requested size,
    evaluated at the requested AREA (floor/ceiling-clamped). Concretely we
    compute the ideal continuous ``(W*, H*)`` that has the requested aspect and
    the desired area, uniformly scale it under the edge ceiling, then snap BOTH
    axes to the 16px grid and pick the legal grid neighbour minimising
    ``(aspect_error, area_error)`` around that point.

    Two failure modes this avoids:

      - Ceiling drift (original IMG-02): a global "minimise aspect error" sweep
        drifts to the pixel ceiling because a huge grid point approximates an
        arbitrary ratio more precisely. Fixed by making AREA the primary
        objective, anchored to the requested area.
      - Near-legal collapse (review #4): the previous fix capped candidates at
        ``NORMALIZE_MAX_AREA_INFLATION × smallest_in_tolerance_area``. For a
        near-legal high-resolution request (``3000x2000`` ≈ 6 MP) the smallest
        in-tolerance grid point is ~1.3 MP, so the cap threw away the requested
        resolution and collapsed it to ~1.3 MP — while a one-pixel-different
        ``3008x2000`` passed through untouched, a large quality discontinuity.
        Fixed by anchoring to the requested area directly (no relative cap), so
        a ~6 MP request maps to ~6 MP.

    >3:1 ratios are handled symmetrically: the target ratio is clamped into
    ``[1/3, 3]`` up front, so ``1x1548`` and ``1548x1`` produce transpose-equal
    results (a 1:3 / 3:1 legal shape), not the old asymmetric width-only sweep.
    """
    try:
        w_str, _, h_str = str(size).lower().replace(" ", "").partition("x")
        w, h = float(int(w_str)), float(int(h_str))
    except (ValueError, AttributeError):
        return DEFAULT_SIZE
    if w <= 0 or h <= 0:
        return DEFAULT_SIZE

    step = GPT_IMAGE_2_EDGE_STEP
    lo, hi = step, GPT_IMAGE_2_MAX_EDGE  # legal per-edge range (both multiples of 16)

    # Clamp the requested aspect into the legal 3:1 window SYMMETRICALLY, so an
    # impossible portrait (1:1548) and its transpose (1548:1) map to mirror-image
    # legal shapes (3:1 ↔ 1:3) instead of the old asymmetric width-only result.
    requested_ratio = w / h
    eff_ratio = min(3.0, max(1.0 / 3.0, requested_ratio))  # width : height, in [1/3, 3]

    # Target area: the requested area, clamped to the legal band. A below-floor
    # mobile canvas lands just above the floor (never inflated to the ceiling); a
    # near-legal high-res request keeps (close to) its own area (no collapse).
    desired_area = min(GPT_IMAGE_2_MAX_PIXELS, max(GPT_IMAGE_2_MIN_PIXELS, w * h))

    def _clamp_edge(v: float) -> int:
        snapped = int(round(v / step) * step)
        return max(lo, min(hi, snapped))

    # Ideal continuous dimensions with the effective aspect at the desired area:
    #   W*/H* = eff_ratio,  W*·H* = desired_area
    #   ⇒ W* = sqrt(desired_area · eff_ratio),  H* = sqrt(desired_area / eff_ratio)
    # If either edge would exceed the max-edge cap, the desired area isn't
    # reachable at this aspect — clamp the long edge and let area fall out (the
    # candidate search still repairs it into the legal band).
    w_ideal = math.sqrt(desired_area * eff_ratio)
    h_ideal = math.sqrt(desired_area / eff_ratio)
    if max(w_ideal, h_ideal) > hi:
        edge_scale = hi / max(w_ideal, h_ideal)
        w_ideal *= edge_scale
        h_ideal *= edge_scale
        desired_area = w_ideal * h_ideal

    def _fix_ratio(a: int, b: int) -> tuple[int, int]:
        # Ensure max(a, b) / min(a, b) <= 3 by raising the shorter edge.
        if min(a, b) * 3 < max(a, b):
            if a >= b:
                b = _clamp_edge(a / 3.0)
            else:
                a = _clamp_edge(b / 3.0)
        return a, b

    def _repair(a: int, b: int) -> tuple[int, int] | None:
        """Snap (a, b) into the fully legal band; None if impossible."""
        a, b = _fix_ratio(a, b)
        # pixel ceiling: shrink the longer edge on the grid until area fits
        while a * b > GPT_IMAGE_2_MAX_PIXELS:
            if a >= b and a > lo:
                a -= step
            elif b > lo:
                b -= step
            else:
                break
            a, b = _fix_ratio(a, b)
        # pixel floor: grow the shorter edge on the grid until area is met
        while a * b < GPT_IMAGE_2_MIN_PIXELS:
            if a <= b and a < hi:
                a += step
            elif b < hi:
                b += step
            else:
                break
            a, b = _fix_ratio(a, b)
        return (a, b) if is_valid_gpt_image_2_size(f"{a}x{b}") else None

    # Local search: try the grid neighbours around the ideal point (each axis
    # snapped down/up), repair each into the legal band, and score by
    # (aspect error to eff_ratio, area error to desired_area). This is anchored
    # at the area-correct ideal, so it can neither drift to the ceiling nor
    # collapse a near-legal high-res request.
    base_w = int(w_ideal // step) * step
    base_h = int(h_ideal // step) * step
    candidates: list[tuple[float, float, int, int]] = []
    seen: set[tuple[int, int]] = set()
    for dw in (0, step, 2 * step):
        for dh in (0, step, 2 * step):
            cand = _repair(_clamp_edge(base_w + dw), _clamp_edge(base_h + dh))
            if cand is None or cand in seen:
                continue
            seen.add(cand)
            a, b = cand
            area_err = abs(a * b - desired_area)
            ratio_err = abs(math.log((a / b) / eff_ratio))
            candidates.append((ratio_err, area_err, a, b))

    if not candidates:
        # Fall back to a full grid sweep only if the local search somehow found
        # nothing legal (should not happen for positive input) — never emit an
        # illegal size.
        for wi in range(lo, hi + 1, step):
            cand = _repair(wi, _clamp_edge(wi / eff_ratio))
            if cand is None:
                continue
            a, b = cand
            candidates.append(
                (
                    abs(math.log((a / b) / eff_ratio)),
                    abs(a * b - desired_area),
                    a,
                    b,
                )
            )
    if not candidates:
        return DEFAULT_SIZE

    best = min(candidates, key=lambda c: (c[0], c[1]))
    result = f"{best[2]}x{best[3]}"
    # Hard postcondition: the function name promises a legal size.
    assert is_valid_gpt_image_2_size(result), (
        f"normalize_size produced illegal {result!r}"
    )
    return result


def generate_image(
    config: Config,
    *,
    prompt: str,
    output: Path,
    size: str = "auto",
    quality: str = "low",
    max_retries: int = 3,
    retry_delay: float = 10.0,
    safe_bg_color: str | None = None,
    enforce_safe_bg_avoidance: bool = True,
    subject_margin: bool = True,
    flat_2d: bool = False,
    framing_profile: str | None = None,
    asset_type: str | None = None,
    aspect: str | None = None,
) -> dict[str, Any]:
    """Text-to-image with rate-limit retry. Returns ``{output, size}``.

    ``size`` defaults to ``"auto"``: the actual W×H is then derived from
    ``aspect`` / ``asset_type`` via :func:`resolve_size` so callers don't
    pay for a 1024² square on wide backgrounds or tiny icons. An explicit
    ``size`` (e.g. ``"1536x864"``) always overrides the adaptive pick.

    ``enforce_safe_bg_avoidance`` (default True) prepends the
    SAFE_BG_AVOID_BLOCK to ``prompt`` so the asset stays clear of the selected
    chroma-key color without forbidding the alternate color. Pass
    ``safe_bg_color=#FF00FF`` / ``#00F000`` to request that the canvas be
    filled with that solid color (ready for Seedance i2v + matting).

    Framing is decided by asset ROLE, not bg color: an explicit
    ``framing_profile`` (``grounded_sprite``/``centered_asset``/``full_scene``)
    wins; otherwise the default is derived from ``asset_type`` (grounded
    character types → foot-anchor block; opaque scene types → no framing block;
    everything else → centered-with-margin). ``subject_margin=False`` opts a
    matted-type asset out of the single-subject block.
    """
    image_defaults = config.assets.image
    size = resolve_size(
        size,
        asset_type=asset_type,
        aspect=aspect,
        size_by_asset_type=image_defaults.size_by_asset_type,
        size_by_aspect=image_defaults.size_by_aspect,
    )
    # An explicit caller size (e.g. a game's real canvas like "960x540") may not be
    # gpt-image-2-legal — snap it to the nearest valid WxH instead of letting the
    # API reject it. "auto" and already-legal sizes pass through unchanged.
    if size != "auto" and not is_valid_gpt_image_2_size(size):
        size = normalize_size(size)

    if (
        enforce_safe_bg_avoidance
        or safe_bg_color is not None
        or subject_margin
        or flat_2d
    ):
        prompt = wrap_prompt_with_safe_bg(
            prompt,
            safe_bg_color=safe_bg_color,
            framing_profile=framing_profile,
            asset_type=asset_type,
            subject_margin=subject_margin,
            flat_2d=flat_2d,
        )

    # The provider client owns endpoint, authentication, and wire details; the
    # adapter keeps prompt wrapping, sizing, and artifact writes.
    provider = get_provider(config)
    size = provider.image_output_size(config, size)
    result = provider.generate_image(
        config,
        prompt=prompt,
        size=size,
        quality=quality,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result["bytes"])
    return {
        "output": str(output),
        "size": size,
        "quality": quality,
        "request_id": result.get("request_id"),
    }


def edit_image(
    config: Config,
    *,
    prompt: str,
    input_path: str,
    output: Path,
    size: str = "1024x1024",
    quality: str = "low",
    safe_bg_color: str | None = None,
    enforce_safe_bg_avoidance: bool = True,
    subject_margin: bool = True,
    flat_2d: bool = False,
    framing_profile: str | None = None,
    asset_type: str | None = None,
) -> dict[str, Any]:
    """Image edit (reference-based).

    Framing follows the same role-based rule as :func:`generate_image`: an
    explicit ``framing_profile`` wins, else it is derived from ``asset_type``.
    """
    if size != "auto" and not is_valid_gpt_image_2_size(size):
        size = normalize_size(size)

    if (
        enforce_safe_bg_avoidance
        or safe_bg_color is not None
        or subject_margin
        or flat_2d
    ):
        prompt = wrap_prompt_with_safe_bg(
            prompt,
            safe_bg_color=safe_bg_color,
            framing_profile=framing_profile,
            asset_type=asset_type,
            subject_margin=subject_margin,
            flat_2d=flat_2d,
        )

    provider = get_provider(config)
    size = provider.image_output_size(config, size)
    result = provider.edit_image(
        config,
        prompt=prompt,
        size=size,
        quality=quality,
        input_path=input_path,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result["bytes"])
    return {
        "output": str(output),
        "size": size,
        "request_id": result.get("request_id"),
    }


__all__ = [
    "CHARACTER_FOOT_ANCHOR_BLOCK",
    "DEFAULT_SIZE",
    "FLAT_2D_SCENE_BLOCK",
    "FRAMING_PROFILES",
    "FULL_SCENE_ASSET_TYPES",
    "GROUNDED_SPRITE_ASSET_TYPES",
    "OPAQUE_SCENE_ASSET_TYPES",
    "SAFE_BG_AVOID_BLOCK",
    "SAFE_BG_COLORS",
    "default_framing_profile",
    "default_safe_bg_color",
    "edit_image",
    "generate_image",
    "is_valid_gpt_image_2_size",
    "normalize_size",
    "resolve_framing_profile",
    "resolve_size",
    "wrap_prompt_with_safe_bg",
]
