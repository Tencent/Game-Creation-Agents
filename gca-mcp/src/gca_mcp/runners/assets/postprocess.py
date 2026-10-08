#!/usr/bin/env python3
"""
Post-process generated game assets: layered preview composition and seamless
tile fixing.

This script never removes backgrounds and never splits grids: use RMBG2 matting
(`rmbg2_matting` / `rmbg2_video_matting`) for transparency and the i2v /
`produce_static_asset` pipelines to generate individual assets.

Subcommands:
  compose-preview Compose a layered preview from base + prop placements
  make-seamless   Offset-blend a tile so it repeats without a visible seam

Usage Examples:
  # Compose a preview from base map + prop placements
  python postprocess.py compose-preview --base map-base.png \
    --placements props.json --output preview.png

  # Make a tile seamless for TileSprite repetition
  python postprocess.py make-seamless --input tile.png --output tile-seamless.png

Dependencies: Pillow (PIL), numpy
  pip install Pillow numpy
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError:
    print("ERROR: 'Pillow' and 'numpy' packages required.")
    print("Install with: pip install Pillow numpy")
    import sys
    sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: compose-preview
# ---------------------------------------------------------------------------

def cmd_compose_preview(args: argparse.Namespace) -> None:
    """Compose a layered preview from a base image + prop placement JSON."""
    base = Image.open(args.base).convert("RGBA")
    data = json.loads(Path(args.placements).read_text(encoding="utf-8"))

    # Support various JSON structures
    if isinstance(data, list):
        props = data
    elif isinstance(data, dict):
        for key in ("props", "foreground", "objects"):
            if key in data and isinstance(data[key], list):
                props = data[key]
                break
        else:
            raise ValueError("Placement JSON must be a list or contain 'props'/'objects' list.")
    else:
        raise ValueError("Unexpected JSON structure.")

    roots = [Path(args.placements).parent, Path(args.base).parent, Path.cwd()]

    # Sort by y for proper overlap (painter's algorithm)
    props.sort(key=lambda p: float(p.get("sortY", p.get("y", 0))))

    pasted = []
    for prop in props:
        image_key = prop.get("image") or prop.get("path")
        if not image_key:
            continue

        # Resolve path
        img_path = Path(image_key)
        if not img_path.is_absolute():
            for root in roots:
                candidate = root / img_path
                if candidate.exists():
                    img_path = candidate
                    break

        if not img_path.exists():
            print(f"WARNING: Prop image not found: {img_path}")
            continue

        img = Image.open(img_path).convert("RGBA")
        w = int(prop.get("w", prop.get("width", img.width)))
        h = int(prop.get("h", prop.get("height", img.height)))
        if (w, h) != img.size:
            img = img.resize((w, h), Image.Resampling.LANCZOS)

        # Compute position based on anchor
        anchor = str(prop.get("anchor", "center-bottom"))
        x = float(prop.get("x", 0))
        y = float(prop.get("y", 0))

        if anchor == "top-left":
            left, top = int(x), int(y)
        elif anchor == "center":
            left, top = int(x - w / 2), int(y - h / 2)
        elif anchor == "bottom-left":
            left, top = int(x), int(y - h)
        else:  # center-bottom (default)
            left, top = int(x - w / 2), int(y - h)

        base.alpha_composite(img, (left, top))
        pasted.append({
            "id": prop.get("id", img_path.stem),
            "image": str(img_path),
            "left": left, "top": top, "w": w, "h": h,
        })

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    base.save(output)

    # Optional report
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            "base": str(args.base),
            "placements": str(args.placements),
            "output": str(output),
            "pasted": pasted,
        }, indent=2), encoding="utf-8")

    print(f"Preview saved: {output} ({len(pasted)} props placed)")


# ---------------------------------------------------------------------------
# Subcommand: make-seamless
# ---------------------------------------------------------------------------

def cmd_make_seamless(args: argparse.Namespace) -> None:
    """Make a middle/repeatable tile horizontally (and optionally vertically)
    seamless via offset-blend.

    AI-generated tiles are never truly tileable: butting two copies edge to
    edge shows a hard seam. We fix it deterministically (no API, no model):
    roll the image by half its size so the original seam lands in the centre,
    then cross-fade a feather-width band over that centre with the un-rolled
    copy. The result wraps cleanly, so `TileSprite`/texture-repeat shows no
    break. This is what makes the implement-side stitching (buildPlatform /
    buildPipe) actually look continuous instead of "images dumped together".
    """
    img = Image.open(args.input).convert("RGBA")
    arr = np.asarray(img, dtype=np.float32)
    h, w = arr.shape[:2]

    def _blend_axis(a: np.ndarray, axis: int, feather: int) -> np.ndarray:
        n = a.shape[axis]
        feather = max(1, min(feather, n // 2))
        # Roll by half so the (formerly edge) seam sits at the centre.
        rolled = np.roll(a, n // 2, axis=axis)
        # Weight ramp across the feather band, centred on the seam:
        # 1 at the band edges (match the un-rolled outside), 0 at the seam
        # centre (replace the discontinuity with the original continuous mid).
        ramp = np.linspace(0.0, 1.0, feather, dtype=np.float32)
        weight = np.concatenate([ramp[::-1], ramp])           # 1→0→1
        start = n // 2 - feather
        w_full = np.ones(n, dtype=np.float32)
        w_full[start:start + 2 * feather] = weight
        shape = [1, 1, 1]
        shape[axis] = n
        w_full = w_full.reshape(shape)
        return rolled * w_full + a * (1.0 - w_full)

    feather = args.feather
    out = _blend_axis(arr, axis=1, feather=feather)            # horizontal
    if args.both_axes:
        out = _blend_axis(out, axis=0, feather=feather)        # vertical too

    result = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.save(args.output)
    print(f"Seamless tile written: {args.output} ({w}x{h}, feather={feather})")
    print(str(args.output))


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Post-process generated game assets: preview composition "
                    "and seamless-tile fixing. Does not remove backgrounds or "
                    "split grids.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- compose-preview ---
    p_preview = subparsers.add_parser(
        "compose-preview",
        help="Compose a layered preview from base + prop placements",
    )
    p_preview.add_argument("--base", required=True, type=Path, help="Base/background image")
    p_preview.add_argument("--placements", required=True, type=Path, help="Placement JSON file")
    p_preview.add_argument("--output", required=True, type=Path, help="Output preview image")
    p_preview.add_argument("--report", type=Path, help="Optional report JSON path")

    # make-seamless: offset-blend a tile so it repeats without a visible seam
    p_seam = subparsers.add_parser(
        "make-seamless",
        help="Make a middle/repeatable tile seamless via offset-blend (no API).",
    )
    p_seam.add_argument("--input", required=True, type=Path, help="Input tile image")
    p_seam.add_argument("--output", required=True, type=Path, help="Output seamless tile")
    p_seam.add_argument("--feather", type=int, default=16,
                        help="Cross-fade band half-width in px across the seam (default 16)")
    p_seam.add_argument("--both-axes", action="store_true",
                        help="Also make the vertical edges seamless (default: horizontal only)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "compose-preview":
        cmd_compose_preview(args)
    elif args.command == "make-seamless":
        cmd_make_seamless(args)


if __name__ == "__main__":
    main()
