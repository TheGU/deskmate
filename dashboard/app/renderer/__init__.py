"""HTML to six-color PNG rendering.

Only the palette is re-exported here. ``app/renderer/render.py`` is not,
deliberately: importing it pulls in Playwright, ``app/view.py`` and the
module registry, and ``app/view.py`` itself imports ``app.renderer.chart``,
so a package ``__init__`` that reached for the renderer would close that
loop. Import :class:`app.renderer.render.Renderer` from its own module.
"""

from __future__ import annotations

from app.renderer.palette import (
    PALETTE,
    PALETTE_RGB,
    assert_palette,
    palette_image,
    quantize,
    to_png_bytes,
)

__all__ = [
    "PALETTE",
    "PALETTE_RGB",
    "assert_palette",
    "palette_image",
    "quantize",
    "to_png_bytes",
]
