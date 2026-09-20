"""The ``ha_dashboard`` page's own renderer: a screenshot of a live Home
Assistant Lovelace view instead of a template
(``app/modules/__init__.py:ScreenshotFn``).

The browser context this opens is a throwaway: one navigation, one settle,
one screenshot, then closed, always in ``finally``. A blank URL, a timeout, a
network error or a redirect back to Home Assistant's own login page all
become the same thing on the panel: a plain frame naming what went wrong,
never a stale screenshot from a previous, working render. The URL and the
token are never logged; only the failure's class is.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from functools import lru_cache
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from app.config import APP_DIR
from app.logging_setup import log
from app.modules.ha_dashboard.settings import HaDashboardSettings
from app.renderer.palette import DISPLAY_SIZE, PALETTE

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from playwright.async_api import Browser

    from app.models import DashboardState
    from app.settings import HubSettings

logger = logging.getLogger("app.modules.ha_dashboard")

#: The whole call (new context, navigation, settle, screenshot) is bounded to
#: this many seconds no matter what hangs inside it (the plan's "its whole
#: path is bounded to 8 s"). The steps below have their own, smaller
#: timeouts; this is the backstop for whatever those miss.
TOTAL_TIMEOUT_SECONDS: float = 8.0

#: Navigation to the dashboard itself (the plan's "navigation timeout 4 s").
NAVIGATION_TIMEOUT_MS: float = 4000

#: The screenshot call, after settling: short, because by then the page has
#: already loaded and settled and there is nothing left to wait for.
SCREENSHOT_TIMEOUT_MS: float = 2000

_FONT_PATH = APP_DIR / "static" / "fonts" / "GoogleSans-LatinThai-var.ttf"
_ERROR_FONT_SIZE = 28


class _NoUrlError(Exception):
    """``dashboard_url`` is blank."""


class _LoginPageError(Exception):
    """The final URL landed on Home Assistant's own login page."""


async def screenshot(
    browser: "Browser", state: "DashboardState", settings: "HubSettings"
) -> Image.Image:
    """Screenshot the configured dashboard, or draw why it could not be.

    ``state`` is unused: this page has no dataset of its own (the plan's
    "page only, no dataset"; the REST ``home`` dataset stays the System
    page's source, ``app/modules/home/``) so it draws whatever the live
    dashboard shows rather than anything in ``DashboardState``.
    """
    del state
    config: HaDashboardSettings = settings.ha_dashboard
    try:
        return await asyncio.wait_for(_render(browser, config), timeout=TOTAL_TIMEOUT_SECONDS)
    except _NoUrlError:
        log(logger, logging.INFO, "ha_dashboard has no dashboard_url configured")
        return _error_frame("HA dashboard: no URL configured")
    except _LoginPageError:
        log(logger, logging.WARNING, "ha_dashboard landed on the Home Assistant login page")
        return _error_frame("HA dashboard: login page, check the token")
    except (TimeoutError, PlaywrightTimeoutError):
        # Both the 8 s backstop (asyncio.wait_for, a plain TimeoutError since
        # Python 3.11) and Playwright's own, shorter navigation/screenshot
        # timeouts land here: from the panel's point of view both are "it
        # took too long", so both get the bound this module documents.
        log(logger, logging.WARNING, "ha_dashboard render timed out")
        return _error_frame(f"HA dashboard: timed out after {int(TOTAL_TIMEOUT_SECONDS)} s")
    except PlaywrightError:
        log(logger, logging.WARNING, "ha_dashboard hit a network error")
        return _error_frame("HA dashboard: network error, check the URL")
    except Exception as exc:  # noqa: BLE001 - any other failure is an error frame, never a stale one
        log(logger, logging.WARNING, "ha_dashboard render failed", error_type=type(exc).__name__)
        return _error_frame("HA dashboard: could not load the dashboard")


async def _render(browser: "Browser", config: HaDashboardSettings) -> Image.Image:
    # Deferred: render.py builds its module-level PAGES tuple by importing
    # every built-in module (this one included) before SUPERSAMPLE is
    # defined in that file (app/renderer/render.py), so importing it at this
    # module's own load time would be a circular import that fails with a
    # partially-initialized module error. By the time this function actually
    # runs, a render is already underway and app.renderer.render is fully
    # loaded (see app/view.py:enabled_pages for the same pattern).
    from app.renderer.render import SUPERSAMPLE

    if not config.dashboard_url:
        raise _NoUrlError

    context = await browser.new_context(
        viewport={"width": DISPLAY_SIZE[0], "height": DISPLAY_SIZE[1]},
        device_scale_factor=SUPERSAMPLE,
        color_scheme="light",
        reduced_motion="reduce",
        forced_colors="none",
    )
    try:
        origin = _origin_of(config.dashboard_url)
        await context.add_init_script(_init_script(origin, config.token.get_secret_value()))
        page = await context.new_page()
        await page.goto(config.dashboard_url, timeout=NAVIGATION_TIMEOUT_MS, wait_until="load")
        if "/auth/" in page.url:
            raise _LoginPageError
        await page.wait_for_timeout(config.settle_ms)
        raw = await page.screenshot(
            type="png",
            clip={"x": 0, "y": 0, "width": DISPLAY_SIZE[0], "height": DISPLAY_SIZE[1]},
            animations="disabled",
            caret="hide",
            timeout=SCREENSHOT_TIMEOUT_MS,
        )
    finally:
        await context.close()

    image = Image.open(io.BytesIO(raw))
    image.load()
    # Chromium screenshots at SUPERSAMPLE x the panel resolution (the
    # device_scale_factor above); render.py's own template path downsamples
    # with the same filter after its screenshot, and does not do so again
    # for a page with a ScreenshotFn (app/renderer/render.py:render_rgb), so
    # this module hands back exactly what the quantize step expects: an
    # 800x480 image, not a supersampled one.
    return image.convert("RGB").resize(DISPLAY_SIZE, Image.LANCZOS)


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _init_script(origin: str, token: str) -> str:
    """The localStorage write sibbl/hass-lovelace-kindle-screensaver makes
    before it navigates to a dashboard (``home-assistant-auth.js``,
    ``createAuthenticatedContext``, fetched 2026-09-20): a ``hassTokens``
    entry with just enough of the frontend's token shape for it to treat the
    tab as already logged in --- ``hassUrl``, ``access_token`` and
    ``token_type: "Bearer"``, and nothing else (no ``expires``,
    ``expires_in`` or ``clientId``: the frontend's own expiry check compares
    against ``undefined`` and never trips) --- plus the ``selectedLanguage``
    key that project sets alongside it so the page does not show its own
    language picker before it draws the dashboard.
    """
    hass_tokens = json.dumps({"hassUrl": origin, "access_token": token, "token_type": "Bearer"})
    return (
        f"localStorage.setItem('hassTokens', {json.dumps(hass_tokens)});"
        f"localStorage.setItem('selectedLanguage', {json.dumps(json.dumps('en'))});"
    )


@lru_cache(maxsize=4)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT_PATH), size)


def _error_frame(message: str) -> Image.Image:
    """A plain frame naming what went wrong, at the size the quantize step
    expects (``app/renderer/palette.py:DISPLAY_SIZE``) --- never a stale
    screenshot from a previous, working render.
    """
    image = Image.new("RGB", DISPLAY_SIZE, PALETTE["white"])
    draw = ImageDraw.Draw(image)
    font = _font(_ERROR_FONT_SIZE)
    left, top, right, bottom = draw.textbbox((0, 0), message, font=font)
    width, height = right - left, bottom - top
    x = max(16, (DISPLAY_SIZE[0] - width) / 2)
    y = (DISPLAY_SIZE[1] - height) / 2
    draw.text((x, y), message, font=font, fill=PALETTE["black"])
    return image


__all__ = ["screenshot"]
