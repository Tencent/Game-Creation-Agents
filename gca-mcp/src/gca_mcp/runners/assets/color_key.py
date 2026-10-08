#!/usr/bin/env python3
"""Pure-computation matting for the two supported staging-background colors.

Deterministic CPU-only fallback for environments without an RMBG-2.0 deployment.
No model, no API: it clears pixels near the selected key globally, then
flood-fills background-connected key-colored pixels from the image borders.
RMBG-2.0 (``rmbg2_matting``) remains the default and produces cleaner soft-alpha
edges; prefer it whenever an endpoint is available.

Usage:
  python color_key.py --input keyed.png --output matted.png
  python color_key.py --input keyed.png --output matted.png --key-color '#00F000'
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import deque
from pathlib import Path

from PIL import Image


SUPPORTED_KEY_COLORS: dict[str, tuple[int, int, int]] = {
    "#FF00FF": (255, 0, 255),
    "#00F000": (0, 240, 0),
}
MAX_THRESHOLD = 255


def validate_settings(
    key_color: str,
    threshold: int,
    edge_threshold: int,
) -> tuple[int, int, int]:
    """Validate and resolve one deterministic color-key configuration."""
    if key_color not in SUPPORTED_KEY_COLORS:
        raise ValueError(
            f"key_color must be one of {tuple(SUPPORTED_KEY_COLORS)}, got {key_color!r}"
        )
    for name, value in (
        ("threshold", threshold),
        ("edge_threshold", edge_threshold),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        minimum = 1 if name == "edge_threshold" else 0
        if value < minimum or value > MAX_THRESHOLD:
            raise ValueError(
                f"{name} must be between {minimum} and {MAX_THRESHOLD}"
            )
    if edge_threshold < threshold:
        raise ValueError("edge_threshold must be greater than or equal to threshold")
    return SUPPORTED_KEY_COLORS[key_color]


def color_distance(
    r: int,
    g: int,
    b: int,
    key_rgb: tuple[int, int, int],
) -> float:
    kr, kg, kb = key_rgb
    return math.sqrt((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2)


def remove_bg_color(
    img: Image.Image,
    *,
    key_color: str,
    threshold: int,
    edge_threshold: int,
) -> Image.Image:
    """Clear the selected staging background from an image.

    Two passes: (1) any non-transparent pixel within ``threshold`` of the key is
    cleared; (2) a border flood-fill clears background-connected pixels within
    ``edge_threshold`` of the key, leaving interior near-key detail intact.

    A successful matte must clear at least one pixel and retain at least one
    visible foreground pixel. This prevents callers from sealing an opaque plate
    when the wrong key was selected, or an empty image when thresholds are bad.
    """
    key_rgb = validate_settings(key_color, threshold, edge_threshold)
    img = img.convert("RGBA")
    pixels = img.load()
    width, height = img.size
    cleared_pixels = 0

    for x in range(width):
        for y in range(height):
            r, g, b, a = pixels[x, y]
            if a != 0 and color_distance(r, g, b, key_rgb) < threshold:
                pixels[x, y] = (0, 0, 0, 0)
                cleared_pixels += 1

    visited: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))

    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or x < 0 or x >= width or y < 0 or y >= height:
            continue
        visited.add((x, y))
        r, g, b, a = pixels[x, y]
        if a == 0 or color_distance(r, g, b, key_rgb) < edge_threshold:
            if a != 0:
                pixels[x, y] = (0, 0, 0, 0)
                cleared_pixels += 1
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    if (x + dx, y + dy) not in visited:
                        queue.append((x + dx, y + dy))
    if cleared_pixels == 0:
        raise ValueError(
            f"no pixels matched key color {key_color}; refusing to publish an opaque matte"
        )
    if img.getchannel("A").getextrema()[1] == 0:
        raise ValueError("color-key matting removed the entire image foreground")
    return img


def remove_bg_magenta(
    img: Image.Image,
    threshold: int,
    edge_threshold: int,
) -> Image.Image:
    """Backward-compatible wrapper for the original magenta-only helper."""
    return remove_bg_color(
        img,
        key_color="#FF00FF",
        threshold=threshold,
        edge_threshold=edge_threshold,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Keyed input image.")
    parser.add_argument("--output", required=True, type=Path, help="Output RGBA PNG.")
    parser.add_argument(
        "--key-color",
        choices=tuple(SUPPORTED_KEY_COLORS),
        default="#FF00FF",
        help="Staging background color.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=100,
        help="Global key-color removal threshold.",
    )
    parser.add_argument(
        "--edge-threshold",
        type=int,
        default=150,
        help="Border flood-fill key-color threshold.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1
    try:
        with Image.open(args.input) as source:
            matted = remove_bg_color(
                source,
                key_color=args.key_color,
                threshold=args.threshold,
                edge_threshold=args.edge_threshold,
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        matted.save(args.output, format="PNG")
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
