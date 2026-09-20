"""The Today page: priorities, what is next, AI capacity and the brief note.

Today owns no dataset of its own. It draws five that other modules provide
(``needs``), which is exactly why it exists as a module: disabling ``tasks``
leaves Today rendering an unavailable priorities pane rather than crashing,
because ``state.block`` always answers with the asked-for block type.

2.1a registers the page where it already lives (the context builder in
``app/view.py``, the template in ``app/templates``); 2.2 moves both in here.
"""

from __future__ import annotations

from app import __version__
from app.config import APP_DIR
from app.modules import Module, PageSpec
from app.view import PAGE_NAMES, PAGE_PUSH_DATASETS, PAGE_TITLES, today_context, today_flag

MODULE = Module(
    id="today",
    title=PAGE_NAMES["today"],
    version=__version__,
    description="Today's priorities, next events, AI capacity and the brief note.",
    page=PageSpec(
        title=PAGE_TITLES["today"],
        templates_dir=APP_DIR / "templates",
        template="today.html",
        context=today_context,
        render_ttl_seconds=1800.0,
        needs=("tasks", "calendar", "ai_usage", "brief", "weather"),
        demo_datasets=PAGE_PUSH_DATASETS["today"],
        flag=today_flag,
    ),
    default_order=10,
)

__all__ = ["MODULE"]
