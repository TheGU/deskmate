"""The Home Assistant dashboard page: a screenshot of a live Lovelace view
instead of a template. Phase 3 of
docs/plan/2026-09-19-settings-modules-provisioning.md.

This module owns no dataset: the REST ``home`` dataset
(``app/modules/home/``) stays the System page's source, as it always has.
``ha_dashboard`` is a second, independent way to show Home Assistant on the
panel and is off by default, because it needs a dashboard URL and a
long-lived token before it can draw anything honest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app import __version__
from app.config import APP_DIR
from app.modules import Module, PageSpec
from app.modules.ha_dashboard.screenshot import screenshot
from app.modules.ha_dashboard.settings import SECTION, HaDashboardSettings

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.models import DashboardState
    from app.settings import HubSettings

#: Both the window list's name for this page and the big title on the page
#: itself: unlike a template page, the screenshot is the whole page, so
#: there is no separate on-page title to give it.
TITLE = "HOME ASSISTANT"


def _context(state: "DashboardState", settings: "HubSettings") -> dict[str, Any]:
    """No frame of its own to build: the screenshot is the whole page.

    ``PageSpec.context`` still has to be a callable (``app/modules/__init__.py``),
    but core never calls it for a page drawn by ``screenshot`` instead of a
    template (``app/renderer/render.py:Renderer.render_rgb``); this only
    exists so the contract holds if something ever does.
    """
    del state, settings
    return {}


MODULE = Module(
    id="ha_dashboard",
    title=TITLE,
    version=__version__,
    description="A Home Assistant Lovelace view, screenshotted straight to the panel.",
    settings_model=HaDashboardSettings,
    settings_section=SECTION,
    page=PageSpec(
        title=TITLE,
        templates_dir=APP_DIR / "templates",
        context=_context,
        # A fixed number, like every other built-in page's PageSpec, not
        # read off HaDashboardSettings.ttl_seconds: PageSpec.render_ttl_seconds
        # is a plain float set once here, never a callable
        # (app/modules/__init__.py:PageSpec), the same as weather's own
        # ttl_seconds setting has no bearing on its 3600.0 here.
        render_ttl_seconds=300.0,
        screenshot=screenshot,
    ),
    default_order=80,
    default_enabled=False,
)

__all__ = ["MODULE"]
