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

from app.config import Env
from app.logging_setup import log
from app.models import DashboardState
from app.modules import ScreenshotFn
from app.modules.registry import Registry, builtin_registry
from app.renderer.palette import DISPLAY_SIZE, quantize, to_png_bytes
from app.settings import HubSettings
from app.view import ALERT_PAGE, build_context

logger = logging.getLogger("app.render")


def pages_of(registry: Registry) -> tuple[str, ...]:
    """Every page id this hub serves: the enabled module pages, then alert.

    Alert is last and is never a module (``app/modules/__init__.py``:
    ``RESERVED_IDS``); it is not in the window list either, so its position
    here is only about iteration order in tests and in /healthz.
    """
    return (*registry.page_ids(), ALERT_PAGE)


def page_ttls_of(registry: Registry) -> dict[str, float]:
    """Each page's render-cache lifetime. Alert's is 0: it is an interrupt,
    so it is re-rendered whenever it is asked for."""
    return {
        **{module.id: registry.page_ttl_seconds(module.id) for module in registry.pages()},
        ALERT_PAGE: 0.0,
    }


#: The built-in pages, in the order they ship in. Derived from the registry
#: rather than written out, so a new built-in module is one line in
#: ``app/modules/registry.py`` and nothing here. A hub with a module
#: installed into ``DATA_DIR/modules/`` serves more than this: that is
#: ``Renderer.pages``, off the hub's own registry.
PAGES: Final[tuple[str, ...]] = pages_of(builtin_registry())

#: Chromium renders at this multiple of the panel resolution before the
#: Lanczos downsample and the six-ink snap. Curves land closer to their true
#: shape after the six-ink snap when rendered at 4x than at 1x; a panel test
#: on 2026-09-05 preferred 4x, and dithering was rejected on the same test
#: (see renderer/palette.py).
SUPERSAMPLE: Final[int] = 4

#: Per-page render cache lifetime, matching the refresh cadence in the
#: proposal. Each page's own number now lives on its ``PageSpec``; this is
#: the built-in map, the same shape as :data:`PAGES` and derived the same
#: way.
PAGE_TTL_SECONDS: Final[dict[str, float]] = page_ttls_of(builtin_registry())

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
    # Docker's default /dev/shm is 64 MB; this is prophylactic, no crash observed.
    "--disable-dev-shm-usage",
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
    """Renders one page at a time; owns the Chromium process.

    ``hub_settings`` is mutable on purpose: ``Hub.reload()`` (1.2c: also a
    settings save) assigns a fresh snapshot in place rather than replacing
    the whole ``Renderer`` (which would mean tearing down Chromium), so
    ``render_html``/``render_rgb``/``render_png``/``probe`` keep their old
    ``(page, state, ...)`` shape and always read whatever snapshot is
    current at call time.
    """

    def __init__(
        self, env: Env, hub_settings: HubSettings, registry: Registry | None = None
    ) -> None:
        self._env = env
        self.hub_settings = hub_settings
        self._registry = builtin_registry(hub_settings.modules) if registry is None else registry
        self._jinja_env = self._build_environment()
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def _build_environment(self) -> Environment:
        """Core's templates first, then every enabled page's own directory.

        Core first means a module cannot shadow ``base.html`` or the shared
        macros by shipping a file of the same name. The built-in modules all
        point at ``app/templates`` for now (2.2 moves each page's template
        into its package), so for a hub with no third-party module this is
        the same single directory it always was.
        """
        directories = [str(self._env.templates_dir)]
        directories.extend(
            str(directory)
            for directory in self._registry.templates_dirs()
            if str(directory) != str(self._env.templates_dir)
        )
        return Environment(
            loader=FileSystemLoader(directories),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    @property
    def registry(self) -> Registry:
        return self._registry

    @registry.setter
    def registry(self, registry: Registry) -> None:
        """``Hub.reload()`` assigns a rebuilt registry here.

        The Jinja environment goes with it: a module that was just enabled
        brings a template directory the old loader never searched. Chromium
        is untouched, which is the whole reason the renderer is mutated in
        place rather than replaced.
        """
        self._registry = registry
        self._jinja_env = self._build_environment()

    @property
    def pages(self) -> tuple[str, ...]:
        """This hub's page ids, which is not always the built-in :data:`PAGES`."""
        return pages_of(self._registry)

    @property
    def environment(self) -> Environment:
        """The Jinja2 environment, also used for the developer preview page."""
        return self._jinja_env

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
        # The image ships only the headless shell, so this launch must stay
        # headless with no channel.
        self._browser = await self._playwright.chromium.launch(args=_BROWSER_ARGS)
        log(logger, logging.INFO, "chromium started", version=self._browser.version)
        return self._browser

    # -- html ------------------------------------------------------------
    def template_name(self, page: str) -> str:
        """The template file a page is drawn from.

        Core's own alert page is ``alert.html``; a module's page names its
        own file, which is how a third-party module can call its template
        anything as long as it sits in its ``templates_dir``.
        """
        module = self._registry.page(page)
        if module is not None and module.page is not None and module.page.template is not None:
            return module.page.template
        return f"{page}.html"

    def render_html(self, page: str, state: DashboardState, *, embed_fonts: bool) -> str:
        if page not in self.pages:
            raise KeyError(f"unknown page {page!r}")
        context: dict[str, Any] = build_context(
            page, state, self.hub_settings, self._registry.pages()
        )
        context["font_css"] = font_css(str(self._env.static_dir / "fonts"), embed_fonts)
        context["embed_fonts"] = embed_fonts
        template = self._jinja_env.get_template(self.template_name(page))
        return template.render(**context)

    def screenshot_fn(self, page: str) -> ScreenshotFn | None:
        """The page's own RGB renderer, when it has one instead of a template.

        No built-in page does. Phase 3's Home Assistant dashboard module is
        what this dispatch exists for; it is implemented now so a module
        author can rely on the contract before then.
        """
        module = self._registry.page(page)
        if module is None or module.page is None:
            return None
        return module.page.screenshot

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

        A page whose spec carries a ``screenshot`` draws itself instead: it
        is handed the shared browser under the same lock, so its whole path
        is serialized against every other render, and it must hand back an
        800x480 RGB image ready for the six-ink snap.
        """
        screenshot = self.screenshot_fn(page)
        if screenshot is not None:
            async with self._lock:
                browser = await self._ensure_browser()
                image = await screenshot(browser, state, self.hub_settings)
            image = image.convert("RGB")
            # The template path always hands back exactly DISPLAY_SIZE
            # (Chromium's own clip guarantees it, checked above); a module's
            # ScreenshotFn is arbitrary code with no such guarantee, so it
            # gets the same size check and forced resize the template path
            # would have failed loudly without: the six-ink snap and the
            # device both assume 800x480, and a module that draws something
            # else must not silently misdraw the panel.
            if image.size != DISPLAY_SIZE:
                log(
                    logger,
                    logging.WARNING,
                    "module screenshot size did not match the display",
                    page=page,
                    expected=DISPLAY_SIZE,
                    actual=image.size,
                )
                image = image.resize(DISPLAY_SIZE, Image.LANCZOS)
            return image

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
                    timeout=self._env.render_timeout_ms,
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
