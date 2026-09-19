"""AI brief adapters: fixtures and the ``brief`` dataset another agent pushes.

Opening the brief page never triggers an AI request. The hub only shows
whatever ``POST /api/brief`` last stored in the ``brief`` row of the
``datasets`` table (``app/db.py``).

The mode is chosen by the local clock: morning before ``BRIEF_EVENING_HOUR``
(default 14:00), evening after - except for a pushed brief, which is shown
exactly as pushed regardless of the clock (an agent that pushes once a day
does not want its brief to vanish the moment the hour rolls over).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.adapters.base import AdapterUnavailable
from app.adapters.fixtures import day_delta, load_fixture, shift_tree
from app.config import Env
from app.datasets import read_dataset
from app.db import get_database
from app.models import Brief, BriefMode
from app.modules.brief.settings import BriefSettings
from app.modules.general.settings import GeneralSettings
from app.timeutil import now_local, to_local, today_local

logger = logging.getLogger("app.adapters.ai_brief")

DATE_KEYS = frozenset({"generated_at"})


def current_mode(brief: BriefSettings, general: GeneralSettings) -> BriefMode:
    """Morning before ``brief.evening_hour``, evening from that hour on."""
    hour = now_local(general.timezone).hour
    return BriefMode.MORNING if hour < brief.evening_hour else BriefMode.EVENING


class FixtureBriefAdapter:
    """Brief from ``fixtures/brief.json`` (holds both modes)."""

    name = "brief"
    source = "fixture"

    def __init__(self, brief: BriefSettings, general: GeneralSettings, env: Env) -> None:
        self._brief = brief
        self._general = general
        self._env = env

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> Brief:
        env = self._env
        timezone_name = self._general.timezone
        payload = load_fixture(env.fixtures_dir / "brief.json")
        delta = day_delta(
            payload, today_local(timezone_name), enabled=env.fixture_relative_dates
        )
        mode = current_mode(self._brief, self._general)
        raw: Any = payload.get(mode.value)
        if raw is None:
            raw = payload.get("morning") or payload.get("evening")
        if raw is None:
            raise AdapterUnavailable("brief fixture has no morning or evening block")
        brief = Brief.model_validate(shift_tree(raw, delta, DATE_KEYS))
        brief.source = "fixture"
        if brief.generated_at is not None:
            brief.generated_at = to_local(brief.generated_at, timezone_name)
        return brief


class PushBriefAdapter:
    """Brief from the ``brief`` row of the ``datasets`` table: exactly the
    shape ``POST /api/brief`` stores. Unlike the fixture, this never picks
    between a morning and an evening block - the pushed brief is shown as
    pushed until the next push replaces it."""

    name = "brief"
    source = "push"

    def __init__(self, brief: BriefSettings, general: GeneralSettings, env: Env) -> None:
        self._brief = brief
        self._general = general
        self._env = env
        #: Set on every successful fetch: the dataset row's own
        #: ``received_at``. Read by ``CachedAdapter`` for
        #: ``BriefBlock.received_at``.
        self.last_received_at: datetime | None = None

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> Brief:
        database = get_database(self._env.hub_db_file)
        database.migrate()
        found = read_dataset(database, "brief")
        if found is None:
            raise AdapterUnavailable("nothing pushed yet for brief")
        payload, received_at = found
        if not isinstance(payload, dict):
            raise ValueError("the brief dataset must hold an object")
        brief = Brief.model_validate(payload)
        brief.source = "push"
        if brief.generated_at is not None:
            brief.generated_at = to_local(brief.generated_at, self._general.timezone)
        self.last_received_at = to_local(received_at, self._general.timezone)
        return brief


def build_brief_adapter(
    brief: BriefSettings, general: GeneralSettings, env: Env
) -> FixtureBriefAdapter | PushBriefAdapter:
    if brief.source == "push":
        return PushBriefAdapter(brief, general, env)
    return FixtureBriefAdapter(brief, general, env)
