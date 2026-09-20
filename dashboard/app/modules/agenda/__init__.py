"""The Agenda page and the ``calendar`` dataset behind it.

The module id is ``agenda`` because that is the page the device navigates
to; its dataset and its settings section are both called ``calendar``,
because that is what phase 1 named them and what a deployed hub's
``settings`` row is keyed by. A module's own section does not have to be
its id (``app/modules/__init__.py:Module.settings_section``), and renaming
a live row to make it match would be a migration for no gain.
"""

from __future__ import annotations

from app import __version__
from app.adapters.calendar import build_calendar_adapter
from app.config import APP_DIR
from app.models import CalendarBlock
from app.modules import DatasetSpec, Module, PageSpec
from app.modules.calendar.settings import SECTION as CALENDAR_SECTION, CalendarSettings
from app.view import PAGE_NAMES, PAGE_PUSH_DATASETS, PAGE_TITLES, agenda_context, agenda_flag

MODULE = Module(
    id="agenda",
    title=PAGE_NAMES["agenda"],
    version=__version__,
    description="The next seven days: the day's route, the month grid and the list.",
    settings_model=CalendarSettings,
    settings_section=CALENDAR_SECTION,
    datasets=(
        DatasetSpec(
            name="calendar",
            block_model=CalendarBlock,
            value_field="items",
            section=CALENDAR_SECTION,
            build_adapter=lambda calendar, general, context: build_calendar_adapter(
                calendar, general, context.env
            ),
            ttl_seconds=lambda calendar: calendar.ttl_seconds,
        ),
    ),
    page=PageSpec(
        title=PAGE_TITLES["agenda"],
        templates_dir=APP_DIR / "templates",
        template="agenda.html",
        context=agenda_context,
        render_ttl_seconds=1800.0,
        needs=("calendar", "tasks"),
        demo_datasets=PAGE_PUSH_DATASETS["agenda"],
        flag=agenda_flag,
    ),
    default_order=20,
)

__all__ = ["MODULE"]
