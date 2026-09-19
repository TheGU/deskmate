"""Jinja2 to HTML to PNG, through one long-lived Chromium instance.

Playwright is expensive to start, so the process keeps exactly one browser and
one page pool guarded by an ``asyncio.Lock``. Rendering is deterministic:
bundled fonts only, no animation, no network, and the only timestamp on screen
comes from the state itself.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from playwright.async_api import Browser, Playwright, async_playwright

from app.config import Settings
from app.logging_setup import log
from app.models import DashboardState
from app.renderer.palette import DISPLAY_SIZE, quantize, to_png_bytes
from app.view import build_context

logger = logging.getLogger("app.render")

PAGES: Final[tuple[str, ...]] = ("today", "agenda", "weather", "brief", "system", "alert")

#: Chromium renders at this multiple of the panel resolution before the
#: Lanczos downsample and the six-ink snap. Curves land closer to their true
#: shape after the six-ink snap when rendered at 4x than at 1x; a panel test
#: on 2026-09-05 preferred 4x, and dithering was rejected on the same test
#: (see renderer/palette.py).
SUPERSAMPLE: Final[int] = 4

#: Per-page render cache lifetime, matching the refresh cadence in the proposal.
PAGE_TTL_SECONDS: Final[dict[str, float]] = {
    "today": 1800.0,
    "agenda": 1800.0,
    "weather": 3600.0,
    "brief": 300.0,
    "system": 900.0,
    "alert": 0.0,
}

#: (family, ``font-weight`` descriptor, filename). Google Sans is a variable
#: font, so the descriptor is a range and one file covers every weight the
#: pages ask for; it carries Latin and Thai in the same file, so no separate
#: Thai fallback face is needed.
FONT_FACES: Final[tuple[tuple[str, str, str], ...]] = (
    ("Google Sans", "400 700", "GoogleSans-LatinThai-var.ttf"),
    ("Symbols Nerd Font Mono", "400", "SymbolsNerdFontMono-Subset.ttf"),
)

_BROWSER_ARGS: Final[list[str]] = [
    "--force-device-scale-factor=1",
    "--hide-scrollbars",
    "--disable-lcd-text",
    # Full hinting snaps stems to the pixel grid. The panel is 1-bit after
    # quantization, so an unhinted stem lands as a smear of half-tones.
    "--font-render-hinting=full",
    "--disable-gpu",
]


@lru_cache(maxsize=2)
def font_css(fonts_dir: str, embed: bool) -> str:
    """``@font-face`` rules; embedded as data URIs for the offline renderer."""
    directory = Path(fonts_dir)
    rules: list[str] = []
    for family, weight_range, filename in FONT_FACES:
        path = directory / filename
        if not path.is_file():
            log(logger, logging.WARNING, "bundled font missing", path=str(path))
            continue
        if embed:
            payload = base64.b64encode(path.read_bytes()).decode("ascii")
            source = f"url(data:font/ttf;base64,{payload}) format('truetype')"
        else:
            source = f"url('/static/fonts/{filename}') format('truetype')"
        rules.append(
            "@font-face{"
            f"font-family:'{family}';font-style:normal;font-weight:{weight_range};"
            f"font-display:block;src:{source};"
            "}"
        )
    return "\n".join(rules)


class Renderer:
    """Renders one page at a time; owns the Chromium process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._env = Environment(
            loader=FileSystemLoader(str(settings.templates_dir)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def environment(self) -> Environment:
        """The Jinja2 environment, also used for the developer preview page."""
        return self._env

    @property
    def connected(self) -> bool:
        """Whether Chromium is currently up. No lock: is_connected() reads a
        local flag on the Playwright object, and /healthz must answer a
        liveness check without waiting on the render lock."""
        return self._browser is not None and self._browser.is_connected()

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        async with self._lock:
            await self._ensure_browser()

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(args=_BROWSER_ARGS)
        log(logger, logging.INFO, "chromium started", version=self._browser.version)
        return self._browser

    # -- html ------------------------------------------------------------
    def render_html(self, page: str, state: DashboardState, *, embed_fonts: bool) -> str:
        if page not in PAGES:
            raise KeyError(f"unknown page {page!r}")
        context: dict[str, Any] = build_context(page, state, self._settings)
        context["font_css"] = font_css(str(self._settings.static_dir / "fonts"), embed_fonts)
        context["embed_fonts"] = embed_fonts
        template = self._env.get_template(f"{page}.html")
        return template.render(**context)

    async def probe(self, page: str, state: DashboardState, expression: str) -> Any:
        """Evaluate a JavaScript expression against a rendered page.

        The display path never calls this. It exists so a check that only the
        browser can answer, such as whether a bundled font really loaded for
        the glyphs on the page, can be made against the same HTML and the same
        Chromium flags the PNG is screenshotted with.
        """
        html = self.render_html(page, state, embed_fonts=True)
        async with self._lock:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                viewport={"width": DISPLAY_SIZE[0], "height": DISPLAY_SIZE[1]},
                device_scale_factor=1,
                color_scheme="light",
                reduced_motion="reduce",
                forced_colors="none",
            )
            try:
                browser_page = await context.new_page()
                await browser_page.set_content(html, wait_until="load")
                await browser_page.evaluate("document.fonts.ready")
                return await browser_page.evaluate(expression)
            finally:
                await context.close()

    # -- rgb ---------------------------------------------------------------
    async def render_rgb(self, page: str, state: DashboardState) -> Image.Image:
        """The 800x480 RGB stage, before the six-ink snap.

        Chromium renders at ``SUPERSAMPLE`` times the panel resolution
        (device pixel ratio, not the viewport, so the CSS layout and the
        screenshot clip stay in panel-pixel units) and the result is
        downsampled with Lanczos. Curves and diagonals land closer to their
        true shape once the six-ink snap runs on a downsampled image than on
        a 1x screenshot.
        """
        html = self.render_html(page, state, embed_fonts=True)
        async with self._lock:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                viewport={"width": DISPLAY_SIZE[0], "height": DISPLAY_SIZE[1]},
                device_scale_factor=SUPERSAMPLE,
                color_scheme="light",
                reduced_motion="reduce",
                forced_colors="none",
            )
            try:
                browser_page = await context.new_page()
                await browser_page.set_content(html, wait_until="load")
                await browser_page.evaluate("document.fonts.ready")
                raw = await browser_page.screenshot(
                    type="png",
                    full_page=False,
                    animations="disabled",
                    caret="hide",
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": DISPLAY_SIZE[0],
                        "height": DISPLAY_SIZE[1],
                    },
                    timeout=self._settings.render_timeout_ms,
                )
            finally:
                await context.close()

        image = Image.open(io.BytesIO(raw))
        image.load()
        expected_size = (DISPLAY_SIZE[0] * SUPERSAMPLE, DISPLAY_SIZE[1] * SUPERSAMPLE)
        if image.size != expected_size:
            log(
                logger,
                logging.WARNING,
                "screenshot size did not match supersample",
                page=page,
                expected=expected_size,
                actual=image.size,
            )
        return image.convert("RGB").resize(DISPLAY_SIZE, Image.LANCZOS)

    # -- png -------------------------------------------------------------
    async def render_png(self, page: str, state: DashboardState) -> bytes:
        started = time.monotonic()
        image = await self.render_rgb(page, state)
        payload = to_png_bytes(quantize(image))
        log(
            logger,
            logging.INFO,
            "page rendered",
            page=page,
            bytes=len(payload),
            ms=round((time.monotonic() - started) * 1000),
            scale=SUPERSAMPLE,
        )
        return payload
