"""Concept-art helpers for the multimodal GDD (Phase 2 / design-agent).

Two capabilities, both operating on the opaque full-scene *concept mockups*
that design-agent generates under ``design/``:

1. ``fit_to_resolution`` — resize/pad/crop a mockup to the game's EXACT screen
   resolution (GDD Section 1). The image model emits a fixed aspect ladder, so
   a mockup is generated at the nearest legal size and then fitted here so the
   embedded reference matches the real game canvas (no lost HUD / margins).

2. ``ground_and_crop`` — VLM grounding: ask the VLM (the same Doubao image
   path the asset validators use) for bounding boxes of the named UI elements
   / entities visible in a mockup, then crop each region into its own
   reference sub-image. These per-element crops are embedded next to their GDD
   rows and used as ``edit_image`` seeds by assets-agent so final assets match
   the approved concept.

Pillow-only image ops (no extra deps); one VLM round-trip for grounding.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

from ..config import Config
from ..errors import ToolError
from .iterate_vlm import DEFAULT_VLM_MODEL, call_vlm_on_image


# -------------------- fit_to_resolution --------------------

_FIT_MODES = ("cover", "contain", "stretch")


def fit_to_resolution(
    src: Path,
    out: Path,
    *,
    width: int,
    height: int,
    mode: str = "contain",
    pad_color: tuple[int, int, int] = (0, 0, 0),
) -> dict[str, Any]:
    """Fit ``src`` to exactly ``width`` x ``height``.

    - ``contain`` (default): scale to fit inside, pad the remainder. NEVER crops
      content — preferred for concept mockups so the HUD / top / bottom is never
      lost. If ``src`` HAS an alpha channel the padding is TRANSPARENT
      (``(0,0,0,0)``) and alpha is preserved; only fully-opaque sources are
      padded with the opaque ``pad_color``. When ``src`` already matches the
      target aspect (the normal case once mockups are generated at the game's
      real resolution), there are no bars and this is just an exact resize.
    - ``cover``: scale to fill, center-crop the overflow (can drop top/bottom).
    - ``stretch``: resize ignoring aspect (may distort).
    """
    if mode not in _FIT_MODES:
        raise ToolError(
            "INVALID_CONFIG",
            f"mode must be one of {_FIT_MODES}; got {mode!r}.",
        )
    if width <= 0 or height <= 0:
        raise ToolError(
            "INVALID_CONFIG",
            f"width/height must be positive; got {width}x{height}.",
        )
    if not src.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"image not found: {src}")

    try:
        src_img = Image.open(src)
        # Preserve transparency: a matted asset (or any RGBA/paletted-transparent
        # source) must NOT be flattened onto opaque black — that is exactly how
        # letterbox bars get baked into a transparent UI/title asset.
        has_alpha = src_img.mode in ("RGBA", "LA") or (
            src_img.mode == "P" and "transparency" in src_img.info
        )
        img = src_img.convert("RGBA" if has_alpha else "RGB")
    except Exception as e:  # noqa: BLE001
        raise ToolError("INVALID_IMAGE", f"could not open {src}: {e}") from e

    sw, sh = img.size
    if mode == "stretch":
        fitted = img.resize((width, height), Image.LANCZOS)
    elif mode == "cover":
        scale = max(width / sw, height / sh)
        nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
        resized = img.resize((nw, nh), Image.LANCZOS)
        left = (nw - width) // 2
        top = (nh - height) // 2
        fitted = resized.crop((left, top, left + width, top + height))
    else:  # contain
        scale = min(width / sw, height / sh)
        nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
        resized = img.resize((nw, nh), Image.LANCZOS)
        offset = ((width - nw) // 2, (height - nh) // 2)
        if has_alpha:
            fitted = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            fitted.paste(resized, offset, resized)
        else:
            fitted = Image.new("RGB", (width, height), pad_color)
            fitted.paste(resized, offset)

    out.parent.mkdir(parents=True, exist_ok=True)
    fitted.save(out)
    return {
        "output": str(out),
        "width": width,
        "height": height,
        "mode": mode,
        "source_size": f"{sw}x{sh}",
    }


# -------------------- ground_and_crop --------------------

_NORMALIZED_EPSILON = 0.02


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(label).strip().lower()).strip("-")
    return s or "region"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _path_ref(path: Path, canonical: str | None) -> str:
    return canonical if canonical is not None else str(path)


def _child_path_ref(parent: Path, canonical_parent: str | None, name: str) -> str:
    if canonical_parent is None:
        return str(parent / name)
    return (PurePosixPath(canonical_parent) / name).as_posix()


def _require_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolError("INVALID_CONFIG", f"`{name}` must be a positive integer.")


def _require_finite_number(
    value: float,
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> None:
    if isinstance(value, bool):
        raise ToolError("INVALID_CONFIG", f"`{name}` must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(
            "INVALID_CONFIG",
            f"`{name}` must be a finite number.",
        ) from exc
    if not math.isfinite(number):
        raise ToolError("INVALID_CONFIG", f"`{name}` must be finite.")
    if number < minimum or (maximum is not None and number > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise ToolError(
            "INVALID_CONFIG",
            f"`{name}` must be >= {minimum}{upper}.",
        )


def _build_grounding_prompt(elements: list[str] | None, max_elements: int) -> str:
    if elements:
        target = (
            "Locate ONLY these elements in the image:\n"
            + "\n".join(f"- {e}" for e in elements)
            + "\nIf an element is not visible, omit it (do not invent a box)."
        )
    else:
        target = (
            "Detect every distinct, separable visual element a game artist would "
            "re-draw as its own asset: each UI piece (button, panel, health/score "
            "HUD, icon, cursor), each character / enemy / boss, each prop, and any "
            f"standalone VFX. Use short snake_case labels. At most {max_elements} elements."
        )
    return (
        "You are a precise visual grounding tool for a 2D game concept mockup.\n"
        f"{target}\n\n"
        "Return ONLY a JSON array (no prose, no markdown, no code fences). Each "
        "item: {\"label\": \"<snake_case>\", \"box\": [x0, y0, x1, y1]} where the "
        "box is NORMALIZED to 0..1 with the ORIGIN at the TOP-LEFT, x0<x1 (left to "
        "right) and y0<y1 (top to bottom). Boxes must tightly enclose the element.\n"
        "Example: [{\"label\": \"health_bar\", \"box\": [0.02, 0.03, 0.28, 0.09]}]"
    )


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Parse the outer VLM array without mistaking an inner box for completion."""
    s = text.strip()
    # strip ```json ... ``` / ``` ... ``` fences
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    cleaned = re.sub(r",\s*([\]}])", r"\1", s)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            for key in ("elements", "boxes", "regions", "items", "result"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
        if isinstance(parsed, list):
            return parsed
    except ValueError:
        pass

    lo = s.find("[")
    if lo < 0:
        raise ToolError(
            "GROUNDING_FAILED",
            "VLM grounding output did not contain a JSON array.",
            hint=text[:300],
        )

    depth = 0
    in_string = False
    escaped = False
    hi: int | None = None
    for index, char in enumerate(s[lo:], start=lo):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                hi = index
                break
    if hi is None:
        raise ToolError(
            "GROUNDING_FAILED",
            "VLM grounding output ended before the outer JSON array was closed.",
            hint=text[:300],
        )
    candidate = re.sub(r",\s*([\]}])", r"\1", s[lo : hi + 1])
    try:
        parsed = json.loads(candidate)
    except ValueError as e:
        raise ToolError(
            "GROUNDING_FAILED",
            f"VLM grounding output was not parseable JSON: {e}",
            hint=text[:300],
        ) from e
    if not isinstance(parsed, list):
        raise ToolError(
            "GROUNDING_FAILED",
            "VLM grounding output was not a JSON array.",
            hint=text[:300],
        )
    return parsed


def _norm_box(box: Any, w: int, h: int) -> tuple[int, int, int, int] | None:
    """Normalized [x0,y0,x1,y1] (0..1) → clamped pixel box, or None if invalid."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
        return None
    # The prompt contract is normalized coordinates. Clamp only small numeric
    # drift around the boundary; low-valued pixel/percentage-like coordinates
    # such as [0, 0, 2, 1] must not be mistaken for normalized boxes.
    if (
        max(x0, y0, x1, y1) > 1.0 + _NORMALIZED_EPSILON
        or min(x0, y0, x1, y1) < -_NORMALIZED_EPSILON
    ):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(1.0, x1), min(1.0, y1)
    px0, py0 = round(x0 * w), round(y0 * h)
    px1, py1 = round(x1 * w), round(y1 * h)
    if px1 <= px0 or py1 <= py0:
        return None
    return px0, py0, px1, py1


def ground_and_crop(
    config: Config,
    *,
    image_path: Path,
    out_dir: Path,
    elements: list[str] | None = None,
    model: str = DEFAULT_VLM_MODEL,
    padding: float = 0.04,
    min_area_frac: float = 0.0008,
    max_elements: int = 24,
    vlm_max_tokens: int = 1500,
    add_background: bool = True,
    vlm_timeout: float = 120.0,
    source_ref: str | None = None,
    out_dir_ref: str | None = None,
) -> dict[str, Any]:
    """Detect element bboxes in a concept mockup and crop each into its own PNG.

    Returns ``{found, missed, crops, manifest, ...}``. Does NOT raise when an
    individual element is missing — it reports it in ``missed`` so the caller can
    fall back to generating that reference directly.
    """
    _require_positive_int(max_elements, "max_elements")
    _require_positive_int(vlm_max_tokens, "vlm_max_tokens")
    _require_finite_number(padding, "padding", minimum=0.0, maximum=1.0)
    _require_finite_number(
        min_area_frac,
        "min_area_frac",
        minimum=0.0,
        maximum=1.0,
    )
    _require_finite_number(vlm_timeout, "vlm_timeout", minimum=0.000001)
    if not image_path.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"mockup not found: {image_path}")
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ToolError("INVALID_IMAGE", f"could not open {image_path}: {e}") from e
    w, h = img.size

    prompt = _build_grounding_prompt(elements, max_elements)
    raw = call_vlm_on_image(
        config=config,
        image_path=image_path,
        prompt=prompt,
        model=model,
        max_tokens=vlm_max_tokens,
        temperature=0.0,
        timeout=vlm_timeout,
    )
    items = _extract_json_array(raw)
    if not items:
        raise ToolError(
            "GROUNDING_FAILED",
            "VLM grounding returned an empty element array.",
            hint="No background-only waiver is exposed by ground_crop; retry or use explicit references.",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    pad_px = round(padding * max(w, h))
    min_area = min_area_frac * w * h
    source_value = _path_ref(image_path, source_ref)
    source_sha256 = _sha256_file(image_path)

    crops: list[dict[str, Any]] = []
    used: set[str] = set()
    rejected = 0
    for item in items:
        if not isinstance(item, dict):
            rejected += 1
            continue
        label = _slug(item.get("label") or item.get("name") or "region")
        role = _slug(item.get("role") or "")
        if label == "background" or role == "background":
            rejected += 1
            continue
        box = _norm_box(item.get("box") or item.get("bbox"), w, h)
        if box is None:
            rejected += 1
            continue
        x0, y0, x1, y1 = box
        if (x1 - x0) * (y1 - y0) < min_area:
            rejected += 1
            continue
        # apply padding, clamp
        x0, y0 = max(0, x0 - pad_px), max(0, y0 - pad_px)
        x1, y1 = min(w, x1 + pad_px), min(h, y1 + pad_px)
        # de-dupe label collisions
        name = label
        n = 2
        while name in used:
            name = f"{label}-{n}"
            n += 1
        used.add(name)

        crop_path = out_dir / f"{name}.png"
        img.crop((x0, y0, x1, y1)).save(crop_path)
        crops.append(
            {
                "label": name,
                "key": name,
                "crop": crop_path.name,
                "path": _child_path_ref(out_dir, out_dir_ref, crop_path.name),
                "sha256": _sha256_file(crop_path),
                "box_px": [x0, y0, x1, y1],
                "role": item.get("role"),
                "kind": item.get("kind"),
                "source": "vlm",
            }
        )

    grounded_count = len(crops)
    if items and grounded_count == 0:
        raise ToolError(
            "GROUNDING_FAILED",
            "VLM grounding returned elements, but every normalized box was invalid "
            "or below the minimum area.",
            hint=f"candidates={len(items)}, rejected={rejected}",
        )

    if add_background and "background" not in used:
        background_path = out_dir / "background.png"
        img.save(background_path)
        used.add("background")
        crops.append(
            {
                "label": "background",
                "key": "background",
                "crop": background_path.name,
                "path": _child_path_ref(
                    out_dir,
                    out_dir_ref,
                    background_path.name,
                ),
                "sha256": _sha256_file(background_path),
                "box_px": [0, 0, w, h],
                "role": "background",
                "kind": "zone",
                "source": "full_mockup",
            }
        )

    found = [c["label"] for c in crops]
    missed: list[str] = []
    if elements:
        found_slugs = set(found)
        missed = [e for e in elements if _slug(e) not in found_slugs]

    manifest_path = out_dir / "crops.json"
    manifest = {
        "source": source_value,
        "source_sha256": source_sha256,
        "source_size": f"{w}x{h}",
        "bound_to": None,
        "detection_sha256": None,
        "model": model,
        "n_found": len(crops),
        "n_grounded": grounded_count,
        "n_rejected": rejected,
        "crops": crops,
        "missed": missed,
        "waived": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "image": source_value,
        "source_sha256": source_sha256,
        "out_dir": _path_ref(out_dir, out_dir_ref),
        "manifest": _child_path_ref(out_dir, out_dir_ref, manifest_path.name),
        "detection_sha256": None,
        "n_found": len(crops),
        "n_grounded": grounded_count,
        "n_rejected": rejected,
        "found": found,
        "missed": missed,
        "background": add_background,
        "crops": crops,
    }


def crop_from_detect(
    *,
    image_path: Path,
    detect_path: Path,
    out_dir: Path,
    padding: float = 0.02,
    min_area_frac: float = 0.0003,
    add_background: bool = True,
    background_label: str = "background",
    source_ref: str | None = None,
    detect_ref: str | None = None,
    out_dir_ref: str | None = None,
) -> dict[str, Any]:
    """Crop EVERY element from a ``detect_layout`` pass — DETERMINISTIC, key-bound.

    The detect-driven counterpart of :func:`ground_and_crop`. Instead of a second,
    independent VLM grounding (which invents its own labels + granularity and skips
    the background), this reuses the ONE precision ``design/<scene>.detect.json`` so:

    - every detected element is cropped, reusing the SAME ``key`` (crop ``label`` ==
      detect key == §2.0.2 layout key), except the reserved ``background`` key,
      which always denotes the canonical full-mockup crop;
    - a full-frame ``background`` crop is always added for explicit coverage and
      visual-reference accounting; design may seed a §2C base from it only when
      foreground contamination can be removed safely;
    - each crop carries ``role``/``kind`` from detect, so downstream (asset seeding,
      element-visual-diff) can bind + filter by key without an IoU join.

    Boxes come from detect's canvas-normalized ``box`` (× the mockup size), NOT the
    tight ``box_px`` (which may be in a different canvas), then get a little
    ``padding`` so the art seed keeps glows/shadows. crops.json keeps the existing
    schema (``label``/``crop``/``path``/``sha256``/``box_px`` as
    ``[x0,y0,x1,y1]``) so every existing reader (Ref Crop coverage,
    gateVisualDiff, element-visual-diff) works unchanged — the keys are just now
    bound to the layout.
    """
    _require_finite_number(padding, "padding", minimum=0.0, maximum=1.0)
    _require_finite_number(
        min_area_frac,
        "min_area_frac",
        minimum=0.0,
        maximum=1.0,
    )
    if not image_path.is_file():
        raise ToolError("INPUT_FILE_NOT_FOUND", f"mockup not found: {image_path}")
    if not detect_path.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"detect file not found: {detect_path} — run detect_layout first "
            f"(detect-driven crop reuses its measured elements).",
        )
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ToolError("INVALID_IMAGE", f"could not open {image_path}: {e}") from e
    w, h = img.size

    try:
        detect_bytes = detect_path.read_bytes()
        detect = json.loads(detect_bytes)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ToolError("INVALID_IMAGE", f"could not read detect json {detect_path}: {e}") from e
    source_value = _path_ref(image_path, source_ref)
    detect_value = _path_ref(detect_path, detect_ref)
    source_sha256 = _sha256_file(image_path)
    detection_sha256 = hashlib.sha256(detect_bytes).hexdigest()
    if not isinstance(detect, dict):
        raise ToolError(
            "GROUNDING_FAILED",
            f"detect json must be an object: {detect_path}",
        )
    recorded_source = detect.get("source")
    recorded_source_sha256 = detect.get("source_sha256")
    if recorded_source != source_value:
        raise ToolError(
            "GROUNDING_FAILED",
            "Detection source path does not match the requested mockup.",
            hint=f"detect_source={recorded_source!r}, mockup={source_value!r}",
        )
    if recorded_source_sha256 != source_sha256:
        raise ToolError(
            "GROUNDING_FAILED",
            "Detection is stale because the concept mockup content changed.",
            hint=(
                f"recorded_sha256={recorded_source_sha256!r}, "
                f"current_sha256={source_sha256}"
            ),
        )
    det_elements = detect.get("elements") if isinstance(detect, dict) else None
    if not isinstance(det_elements, list):
        raise ToolError(
            "GROUNDING_FAILED",
            f"detect json has no `elements` list: {detect_path}",
        )
    if not det_elements:
        raise ToolError(
            "GROUNDING_FAILED",
            f"detect json contains no elements: {detect_path}",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    pad_px = round(padding * max(w, h))
    min_area = min_area_frac * w * h

    crops: list[dict[str, Any]] = []
    used: set[str] = set()
    skipped: list[str] = []
    missed: list[str] = []
    rejected = 0
    background_key = _slug(background_label)
    for el in det_elements:
        if not isinstance(el, dict):
            rejected += 1
            continue
        key = _slug(el.get("key") or el.get("label") or "element")
        role = _slug(el.get("role") or "")
        if key == background_key or role == "background":
            # A detector background region never satisfies the non-background
            # grounding contract. The only accepted background is the canonical
            # full-mockup reference added after usable elements are confirmed.
            rejected += 1
            continue
        box = el.get("box")  # normalized [x, y, w, h] — resolution-independent
        if not (isinstance(box, (list, tuple)) and len(box) == 4):
            rejected += 1
            missed.append(key)
            continue
        try:
            nx, ny, nbw, nbh = (float(v) for v in box)
        except (TypeError, ValueError):
            rejected += 1
            missed.append(key)
            continue
        if (
            not all(math.isfinite(value) for value in (nx, ny, nbw, nbh))
            or nbw <= 0
            or nbh <= 0
        ):
            rejected += 1
            missed.append(key)
            continue
        raw_box = _norm_box([nx, ny, nx + nbw, ny + nbh], w, h)
        if raw_box is None:
            rejected += 1
            missed.append(key)
            continue
        raw_x0, raw_y0, raw_x1, raw_y1 = raw_box
        if (raw_x1 - raw_x0) * (raw_y1 - raw_y0) < min_area:
            rejected += 1
            skipped.append(key)
            missed.append(key)
            continue
        x0 = max(0, raw_x0 - pad_px)
        y0 = max(0, raw_y0 - pad_px)
        x1 = min(w, raw_x1 + pad_px)
        y1 = min(h, raw_y1 + pad_px)
        name = key
        n = 2
        while name in used:
            name = f"{key}-{n}"
            n += 1
        used.add(name)

        crop_path = out_dir / f"{name}.png"
        img.crop((x0, y0, x1, y1)).save(crop_path)
        crops.append(
            {
                "label": name,
                "key": name,
                "crop": crop_path.name,
                "path": _child_path_ref(out_dir, out_dir_ref, crop_path.name),
                "sha256": _sha256_file(crop_path),
                "box_px": [x0, y0, x1, y1],
                "role": el.get("role"),
                "kind": el.get("kind"),
                "source": "detect",
            }
        )

    grounded_count = len(crops)
    if grounded_count == 0:
        raise ToolError(
            "GROUNDING_FAILED",
            "Detection contains no usable non-background element boxes.",
            hint=f"candidates={len(det_elements)}, rejected={rejected}",
        )

    if add_background and background_key not in used:
        bg_path = out_dir / f"{background_key}.png"
        img.save(bg_path)  # full mockup reference; design decides seed vs explicit waiver
        used.add(background_key)
        crops.append(
            {
                "label": background_key,
                "key": background_key,
                "crop": bg_path.name,
                "path": _child_path_ref(out_dir, out_dir_ref, bg_path.name),
                "sha256": _sha256_file(bg_path),
                "box_px": [0, 0, w, h],
                "role": "background",
                "kind": "zone",
                "source": "full_mockup",
            }
        )

    manifest_path = out_dir / "crops.json"
    manifest = {
        "source": source_value,
        "source_sha256": source_sha256,
        "source_size": f"{w}x{h}",
        "bound_to": detect_value,
        "detection_sha256": detection_sha256,
        "model": "detect-driven",
        "n_found": len(crops),
        "n_grounded": grounded_count,
        "n_rejected": rejected,
        "crops": crops,
        "missed": missed,
        "waived": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "image": source_value,
        "source_sha256": source_sha256,
        "detect": detect_value,
        "detection_sha256": detection_sha256,
        "out_dir": _path_ref(out_dir, out_dir_ref),
        "manifest": _child_path_ref(out_dir, out_dir_ref, manifest_path.name),
        "n_found": len(crops),
        "n_grounded": grounded_count,
        "n_rejected": rejected,
        "found": [c["label"] for c in crops],
        "missed": missed,
        "background": add_background,
        "skipped_below_min_area": skipped,
        "crops": crops,
    }


__all__ = ["fit_to_resolution", "ground_and_crop", "crop_from_detect"]
