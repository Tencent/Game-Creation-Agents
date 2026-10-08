from __future__ import annotations

import pytest
from PIL import Image

from gca_mcp.errors import is_known_code
from gca_mcp.runners.assets.color_key import remove_bg_color


def test_color_key_failure_uses_registered_error_code() -> None:
    assert is_known_code("COLOR_KEY_MATTING_FAILED")
    assert not is_known_code("GRID_QC_FAILED")


@pytest.mark.parametrize(
    ("key_color", "key_rgb"),
    [
        ("#FF00FF", (255, 0, 255)),
        ("#00F000", (0, 240, 0)),
    ],
)
def test_remove_bg_color_supports_both_reserved_keys(
    key_color: str,
    key_rgb: tuple[int, int, int],
) -> None:
    image = Image.new("RGB", (9, 9), key_rgb)
    for x in range(3, 6):
        for y in range(3, 6):
            image.putpixel((x, y), (240, 240, 240))

    result = remove_bg_color(
        image,
        key_color=key_color,
        threshold=100,
        edge_threshold=150,
    )

    assert result.mode == "RGBA"
    assert result.getpixel((0, 0))[3] == 0
    assert result.getpixel((4, 4))[3] == 255
    assert result.getchannel("A").getextrema() == (0, 255)


def test_remove_bg_color_rejects_wrong_key_instead_of_sealing_opaque_plate() -> None:
    green_plate = Image.new("RGBA", (8, 8), (0, 240, 0, 255))

    with pytest.raises(ValueError, match="no pixels matched key color"):
        remove_bg_color(
            green_plate,
            key_color="#FF00FF",
            threshold=100,
            edge_threshold=150,
        )


def test_remove_bg_color_rejects_empty_foreground() -> None:
    key_only = Image.new("RGBA", (8, 8), (255, 0, 255, 255))

    with pytest.raises(ValueError, match="removed the entire image foreground"):
        remove_bg_color(
            key_only,
            key_color="#FF00FF",
            threshold=100,
            edge_threshold=150,
        )


@pytest.mark.parametrize(
    ("key_color", "threshold", "edge_threshold", "message"),
    [
        ("#000000", 100, 150, "key_color"),
        ("#FF00FF", -1, 150, "threshold"),
        ("#FF00FF", 0, 0, "edge_threshold"),
        ("#FF00FF", 100, 256, "edge_threshold"),
        ("#FF00FF", 151, 150, "greater than or equal"),
    ],
)
def test_remove_bg_color_rejects_invalid_settings(
    key_color: str,
    threshold: int,
    edge_threshold: int,
    message: str,
) -> None:
    image = Image.new("RGBA", (8, 8), (255, 0, 255, 255))

    with pytest.raises(ValueError, match=message):
        remove_bg_color(
            image,
            key_color=key_color,
            threshold=threshold,
            edge_threshold=edge_threshold,
        )
