"""HTML to six-color PNG rendering."""

from __future__ import annotations

from app.renderer.palette import (
    PALETTE,
    PALETTE_RGB,
    assert_palette,
    palette_image,
    quantize,
    to_png_bytes,
)
from app.renderer.render import PAGES, Renderer

__all__ = [
    "PAGES",
    "PALETTE",
    "PALETTE_RGB",
    "Renderer",
    "assert_palette",
    "palette_image",
    "quantize",
    "to_png_bytes",
]
