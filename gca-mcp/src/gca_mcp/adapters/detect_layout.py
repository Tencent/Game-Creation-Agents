"""Scene LAYOUT position detection for the multimodal GDD (lays-out-like axis).

This is the position-detection half of the design phase, and is deliberately
DISTINCT from :func:`gca_mcp.adapters.concept_ground.ground_and_crop`:

- ``ground_and_crop`` (looks-like) draws *loose, padded* boxes whose only job is
  to grab an art crop to seed ``edit_image`` — its boxes are explicitly NOT a
  reliable source of *where* an element sits.
- ``detect_layout`` (lays-out-like) runs a precision VLM pass that returns a
  TIGHT, canvas-normalized bounding box + role + suggested anchor for EVERY
  visible element the game must place. It crops NOTHING and writes no PNGs —
  positions only.

The output (``design/<scene>.detect.json``) is the raw measurement the
design-agent turns into the GDD §2.0.2 Scene Layout Design (the authored layout
contract), which scaffold projects into ``.playtest/inputs/layout-contract.json``
and the verify gate (``gateLayoutAlignment``) checks the running game against.

Pillow-only image read (for the canvas size); one VLM round-trip for detection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import Config
from ..errors import ToolError
from .concept_ground import (
    _extract_json_array,
    _norm_box,
    _require_finite_number,
    _require_positive_int,
    _sha256_file,
    _slug,
)
from .iterate_vlm import DEFAULT_VLM_MODEL, call_vlm_on_image

# Roles whose on-screen position is a spawn point / zone rather than a fixed
# static placement (an entity moves at runtime; a zone is a region the game
# fills). The design-agent overrides `kind` per element, but this gives a sane
# default so the contract does not demand a moving sprite stay frozen.
_ENTITY_ROLES = {
    "entity", "actor", "character", "player", "enemy", "boss", "npc",
    "creature", "mob", "vfx", "effect", "projectile",
}
_ZONE_ROLES = {"zone", "region", "area", "field", "lane", "spawn", "spawner"}


def _build_detect_prompt(max_elements: int) -> str:
    """A precision LAYOUT prompt — tight boxes + full coverage + role.

    Intentionally different from concept_ground's crop-grabbing prompt: it asks
    for POSITION (not art), demands tight boxes, and classifies a role so the
    downstream contract can pick a per-role tolerance and a static/spawn/zone
    kind.
    """
    return (
        "You are a precise LAYOUT detector for a single 2D game screen (a "
        "full-scene concept mockup). Your job is POSITION, not art: report WHERE "
        "every distinct on-screen element sits.\n\n"
        "Detect EVERY element the game engine must place on this screen:\n"
        "- HUD widgets: health/energy/boss bars, gauges, score/coin/timer "
        "counters, buttons, selection/build/context panels, minimap frames, "
        "badges, the loading bar\n"
        "- Lettering: the game logo / title wordmark, result art-words "
        "(GAME OVER / VICTORY), styled prompts (TAP TO START), and any static "
        "label/caption\n"
        "- Actors: each character / player / enemy / boss / NPC, at its on-screen "
        "position\n"
        "- Props / interactive objects, and any standalone VFX\n"
        "- A large decorative/background REGION only when it occupies a distinct "
        "area (a framed play-field, a side-panel backdrop) — not the whole sky\n\n"
        f"Return AT MOST {max_elements} elements. Use short snake_case labels that "
        "name the element by role (health_bar, start_button, title_logo, player, "
        "enemy_grunt, score_counter).\n\n"
        "For EACH element return a TIGHT bounding box enclosing ONLY the element's "
        "visible pixels — do NOT pad, do NOT include neighbours, do NOT round out "
        "to a grid cell. Also classify its ROLE.\n\n"
        "Return ONLY a JSON array (no prose, no markdown, no code fences). Each "
        'item: {"label": "<snake_case>", "role": "<hud|bar|gauge|button|panel|'
        'badge|icon|minimap|logo|text|entity|prop|decorative|zone|vfx>", '
        '"box": [x0, y0, x1, y1]} where box is NORMALIZED to 0..1 with the ORIGIN '
        "at the TOP-LEFT, x0<x1 (left to right) and y0<y1 (top to bottom).\n"
        'Example: [{"label": "health_bar", "role": "bar", "box": [0.02,0.03,0.28,0.07]}, '
        '{"label": "player", "role": "entity", "box": [0.10,0.55,0.20,0.80]}]'
    )


def _third(v: float) -> int:
    """0 = first third, 1 = middle, 2 = last third."""
    if v < 1.0 / 3.0:
        return 0
    if v > 2.0 / 3.0:
        return 2
    return 1


def _anchor_and_origin(
    cx: float, cy: float, bw: float, bh: float
) -> tuple[str, list[float]]:
    """Suggest an `anchorTo` edge + matching origin from the box center.

    The design-agent may override, but a sensible default lets a corner widget
    stay pinned to its corner under responsive scaling rather than to absolute
    pixels.
    """
    if bw >= 0.9 and bh >= 0.9:
        return "fill", [0.5, 0.5]
    hx, hy = _third(cx), _third(cy)
    hname = ("left", "center", "right")[hx]
    vname = ("top", "middle", "bottom")[hy]
    if hx == 1 and hy == 1:
        anchor = "center"
    else:
        anchor = f"{vname}-{hname}"
    origin = [round(hx / 2.0, 3), round(hy / 2.0, 3)]
    return anchor, origin


def detect_layout(
    config: Config,
    *,
    image_path: Path,
    out_path: Path | None = None,
    scene: str | None = None,
    canvas: tuple[int, int] | None = None,
    model: str = DEFAULT_VLM_MODEL,
    min_area_frac: float = 0.0003,
    max_elements: int = 32,
    vlm_max_tokens: int = 2500,
    vlm_timeout: float = 120.0,
    source_ref: str | None = None,
    out_path_ref: str | None = None,
) -> dict[str, Any]:
    """Detect every element's tight, canvas-normalized position in a mockup.

    Writes ``design/<scene>.detect.json`` (next to the mockup unless ``out_path``
    is given) and returns a summary. Crops NOTHING — this is the position-only
    pass that seeds the GDD §2.0.2 Scene Layout Design.
    """
    _require_positive_int(max_elements, "max_elements")
    _require_positive_int(vlm_max_tokens, "vlm_max_tokens")
    _require_finite_number(
        min_area_frac,
        "min_area_frac",
        minimum=0.0,
        maximum=1.0,
    )
    _require_finite_number(vlm_timeout, "vlm_timeout", minimum=0.000001)
    if canvas is not None:
        _require_positive_int(canvas[0], "canvas_width")
        _require_positive_int(canvas[1], "canvas_height")
    if not image_path.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"mockup not found: {image_path}")
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ToolError("INVALID_IMAGE", f"could not open {image_path}: {e}") from e
    w, h = img.size
    cw, ch = (int(canvas[0]), int(canvas[1])) if canvas else (w, h)
    source_value = source_ref if source_ref is not None else str(image_path)
    source_sha256 = _sha256_file(image_path)

    raw = call_vlm_on_image(
        config=config,
        image_path=image_path,
        prompt=_build_detect_prompt(max_elements),
        model=model,
        max_tokens=vlm_max_tokens,
        temperature=0.0,
        timeout=vlm_timeout,
    )
    items = _extract_json_array(raw)
    if not items:
        raise ToolError(
            "GROUNDING_FAILED",
            "Layout detection returned an empty element array.",
            hint="Author a normalized layout fallback only after recording this failure.",
        )

    elements: list[dict[str, Any]] = []
    used: set[str] = set()
    rejected = 0
    non_background_count = 0
    for item in items:
        if not isinstance(item, dict):
            rejected += 1
            continue
        # Reuse concept_ground's strict normalized-coordinate parser. The prompt
        # requires 0..1; slight model overshoot is clamped, while pixel/1000
        # coordinates fail closed instead of being guessed.
        px = _norm_box(item.get("box") or item.get("bbox"), w, h)
        if px is None:
            rejected += 1
            continue
        x0p, y0p, x1p, y1p = px
        x0, y0, x1, y1 = x0p / w, y0p / h, x1p / w, y1p / h
        bw, bh = x1 - x0, y1 - y0
        if bw * bh < min_area_frac:
            rejected += 1
            continue
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

        label = _slug(item.get("label") or item.get("name") or "element")
        name = label
        n = 2
        while name in used:
            name = f"{label}-{n}"
            n += 1
        used.add(name)

        role = _slug(item.get("role") or "element")
        if label != "background" and role != "background":
            non_background_count += 1
        kind = (
            "spawn" if role in _ENTITY_ROLES
            else "zone" if role in _ZONE_ROLES
            else "static"
        )
        anchor, origin = _anchor_and_origin(cx, cy, bw, bh)

        elements.append(
            {
                "key": name,            # suggested asset/object key; agent refines
                "role": role,
                "box": [round(x0, 4), round(y0, 4), round(bw, 4), round(bh, 4)],
                # Canvas-space convenience for human review only. Programmatic
                # consumers use normalized `box`, because canvas may differ from
                # the source mockup used by ground_crop.
                "box_canvas_px": [
                    round(x0 * cw), round(y0 * ch),
                    round(bw * cw), round(bh * ch),
                ],
                "center": [round(cx, 4), round(cy, 4)],
                "origin": origin,
                "anchorTo": anchor,
                "kind": kind,
            }
        )

    if not elements:
        raise ToolError(
            "GROUNDING_FAILED",
            "Layout detection returned elements, but every normalized box was invalid "
            "or below the minimum area.",
            hint=f"candidates={len(items)}, rejected={rejected}",
        )
    if non_background_count == 0:
        raise ToolError(
            "GROUNDING_FAILED",
            "Layout detection contains no usable non-background element boxes.",
            hint=f"candidates={len(items)}, rejected={rejected}",
        )

    # Stable ordering: top-to-bottom then left-to-right (reading order) so the
    # authored §2.0.2 table is easy to eyeball against the mockup.
    elements.sort(key=lambda e: (round(e["center"][1], 2), round(e["center"][0], 2)))

    scene_name = scene or image_path.stem
    if out_path is None:
        out_path = image_path.parent / f"{image_path.stem}.detect.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scene": scene_name,
        "source": source_value,
        "source_sha256": source_sha256,
        "source_size": f"{w}x{h}",
        "canvas": {"w": cw, "h": ch},
        "model": model,
        "n_detected": len(elements),
        "n_rejected": rejected,
        "elements": elements,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "image": source_value,
        "source_sha256": source_sha256,
        "scene": scene_name,
        "out_path": out_path_ref if out_path_ref is not None else str(out_path),
        "canvas": {"w": cw, "h": ch},
        "n_detected": len(elements),
        "n_rejected": rejected,
        "elements": elements,
    }


__all__ = ["detect_layout"]
