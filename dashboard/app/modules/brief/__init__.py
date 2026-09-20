"""The ``brief`` section (``settings.py``), its dataset, its page and its push.

The brief is written by an agent and posted to ``POST /api/brief``, so this
module brings the route as well as the adapter. Core mounts the router with
the bearer-token dependency already applied.
"""

from __future__ import annotations

from app import __version__
from app.adapters.ai_brief import build_brief_adapter
from app.config import APP_DIR
from app.models import BriefBlock
from app.modules import DatasetSpec, Module, PageSpec
from app.modules.brief.routes import build_router
from app.modules.brief.settings import SECTION, BriefSettings
from app.view import PAGE_NAMES, PAGE_PUSH_DATASETS, PAGE_TITLES, brief_context, brief_flag

MODULE = Module(
    id="brief",
    title=PAGE_NAMES["brief"],
    version=__version__,
    description="The morning or evening brief an agent writes, plus the task list.",
    settings_model=BriefSettings,
    settings_section=SECTION,
    datasets=(
        DatasetSpec(
            name="brief",
            block_model=BriefBlock,
            value_field="brief",
            section=SECTION,
            build_adapter=lambda brief, general, context: build_brief_adapter(
                brief, general, context.env
            ),
            ttl_seconds=lambda brief: brief.ttl_seconds,
        ),
    ),
    page=PageSpec(
        title=PAGE_TITLES["brief"],
        templates_dir=APP_DIR / "templates",
        template="brief.html",
        context=brief_context,
        render_ttl_seconds=300.0,
        needs=("brief", "tasks"),
        demo_datasets=PAGE_PUSH_DATASETS["brief"],
        flag=brief_flag,
    ),
    routes=build_router,
    default_order=40,
)

__all__ = ["MODULE"]
