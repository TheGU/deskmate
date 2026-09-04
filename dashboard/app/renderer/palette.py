"""Six-color palette handling for the Spectra 6 panel.

The panel shows white, black, red, yellow, green and blue. The server and the
ESPHome driver agree on pure primaries (docs/ARCHITECTURE.md), so the mapping
is exact and no dithering is needed - dithering only produces noise on e-paper.
"""

from __future__ import annotations

import io
from typing import Final

from PIL import Image

#: Canonical panel colors. Order matters: index 0 is the background.
PALETTE: Final[dict[str, tuple[int, int, int]]] = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "yellow": (255, 255, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
}

PALETTE_RGB: Final[tuple[tuple[int, int, int], ...]] = tuple(PALETTE.values())

DISPLAY_SIZE: Final[tuple[int, int]] = (800, 480)


def palette_image() -> Image.Image:
    """A 1x1 ``P`` mode image carrying the six colors, for ``Image.quantize``."""
    flat: list[int] = []
    for red, green, blue in PALETTE_RGB:
        flat.extend((red, green, blue))
    # Pillow wants a 256-entry table; pad with the background color.
    flat.extend(PALETTE_RGB[0] * (256 - len(PALETTE_RGB)))
    reference = Image.new("P", (1, 1))
    reference.putpalette(flat)
    return reference


def quantize(image: Image.Image) -> Image.Image:
    """Snap every pixel to the nearest panel color. Returns an RGB image."""
    source = image.convert("RGB")
    reduced = source.quantize(palette=palette_image(), dither=Image.Dither.NONE)
    return reduced.convert("RGB")


def to_png_bytes(image: Image.Image) -> bytes:
    """Encode as an 8-bit, non-interlaced PNG (what the ESPHome decoder wants)."""
    buffer = io.BytesIO()
    image.convert("RGB").save(
        buffer,
        format="PNG",
        optimize=True,
        interlace=0,
        compress_level=9,
    )
    return buffer.getvalue()


def palette_violations(image: Image.Image) -> set[tuple[int, int, int]]:
    """Colors present in ``image`` that are not part of the panel palette."""
    allowed = set(PALETTE_RGB)
    colors = image.convert("RGB").getcolors(maxcolors=1 << 24) or []
    return {color for _count, color in colors if color not in allowed}


def assert_palette(image: Image.Image) -> None:
    """Raise if the image uses a color the panel cannot show."""
    extra = palette_violations(image)
    if extra:
        sample = sorted(extra)[:8]
        raise AssertionError(
            f"{len(extra)} off-palette color(s), for example {sample}"
        )


def assert_display_image(image: Image.Image) -> None:
    """Raise unless the image is exactly 800x480 and palette-constrained."""
    if image.size != DISPLAY_SIZE:
        raise AssertionError(f"expected {DISPLAY_SIZE}, got {image.size}")
    assert_palette(image)
