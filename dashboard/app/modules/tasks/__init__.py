"""The ``tasks`` section (``settings.py``), its dataset and its push route.

Tasks has no page of its own: Today, Agenda and Brief all draw it. It is a
module because the dataset, its settings and the ``POST /api/tasks`` route
belong together, and because turning it off has to leave those three pages
showing an unavailable pane rather than a stack trace.
"""

from __future__ import annotations

from pathlib import Path

from app import __version__
from app.adapters.tasks import build_tasks_adapter
from app.models import TasksBlock
from app.modules import DatasetSpec, Module
from app.modules.tasks.routes import build_router
from app.modules.tasks.settings import SECTION, TasksSettings

#: The demo data this module falls back to on ``source: fixture``.
FIXTURE: Path = Path(__file__).parent / "fixtures" / "tasks.json"

MODULE = Module(
    id="tasks",
    title="TASKS",
    version=__version__,
    description="The task list an agent pushes, or an Obsidian vault provides.",
    settings_model=TasksSettings,
    settings_section=SECTION,
    datasets=(
        DatasetSpec(
            name="tasks",
            block_model=TasksBlock,
            value_field="items",
            section=SECTION,
            build_adapter=lambda tasks, general, context: build_tasks_adapter(
                tasks, general, context.env, FIXTURE
            ),
            ttl_seconds=lambda tasks: tasks.ttl_seconds,
            fixture=FIXTURE,
        ),
    ),
    routes=build_router,
    default_order=60,
)

__all__ = ["FIXTURE", "MODULE"]
