"""AI quota adapters: fixtures and the ``ai_usage`` dataset another agent
pushes.

There is no supported public API for Claude or Codex quota, so the hub never
scrapes anything. A separate collector posts ``POST /api/ai-usage``; if
nothing has ever been pushed, or the stored row is stale or malformed, the
page prints "unknown". See docs/DATA-SOURCES.md for the schema.
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
from app.models import AIUsage
from app.modules.ai_usage.settings import AIUsageSettings
from app.modules.general.settings import GeneralSettings
from app.timeutil import to_local, today_local

logger = logging.getLogger("app.adapters.ai_usage")

DATE_KEYS = frozenset({"short_window_reset_at", "weekly_reset_at", "collected_at"})


def _localize(providers: list[AIUsage], timezone_name: str) -> list[AIUsage]:
    for provider in providers:
        for field in ("short_window_reset_at", "weekly_reset_at", "collected_at"):
            value = getattr(provider, field)
            if value is not None:
                setattr(provider, field, to_local(value, timezone_name))
    return providers


class FixtureAIUsageAdapter:
    """Quota from ``fixtures/ai_usage.json``."""

    name = "ai_usage"
    source = "fixture"

    def __init__(self, ai_usage: AIUsageSettings, general: GeneralSettings, env: Env) -> None:
        self._ai_usage = ai_usage
        self._general = general
        self._env = env

    def resolve(self) -> str:
        """The source a fetch would use right now, without fetching. Fixed
        for this adapter: it never falls back to anything else."""
        return self.source

    async def fetch(self) -> list[AIUsage]:
        env = self._env
        timezone_name = self._general.timezone
        payload = load_fixture(env.fixtures_dir / "ai_usage.json")
        delta = day_delta(
            payload, today_local(timezone_name), enabled=env.fixture_relative_dates
        )
        raw: Any = shift_tree(payload.get("providers", []), delta, DATE_KEYS)
        providers = [AIUsage.model_validate(item) for item in raw]
        return _localize(providers, timezone_name)


class PushAIUsageAdapter:
    """Quota from the ``ai_usage`` row of the ``datasets`` table, the shape
    ``POST /api/ai-usage`` stores."""

    name = "ai_usage"
    source = "push"

    def __init__(self, ai_usage: AIUsageSettings, general: GeneralSettings, env: Env) -> None:
        self._ai_usage = ai_usage
        self._general = general
        self._env = env
        #: Set on every successful fetch: the dataset row's own
        #: ``received_at``. Read by ``CachedAdapter`` for
        #: ``AIUsageBlock.received_at``.
        self.last_received_at: datetime | None = None

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> list[AIUsage]:
        database = get_database(self._env.hub_db_file)
        database.migrate()
        found = read_dataset(database, "ai_usage")
        if found is None:
            raise AdapterUnavailable("nothing pushed yet for ai_usage")
        payload, received_at = found
        raw: Any
        if isinstance(payload, dict):
            raw = payload.get("providers", [])
        elif isinstance(payload, list):
            raw = payload
        else:
            raise ValueError("the ai_usage dataset must hold an object or a list")
        providers = [AIUsage.model_validate(item) for item in raw]
        self.last_received_at = to_local(received_at, self._general.timezone)
        return _localize(providers, self._general.timezone)


def build_ai_usage_adapter(
    ai_usage: AIUsageSettings, general: GeneralSettings, env: Env
) -> FixtureAIUsageAdapter | PushAIUsageAdapter:
    if ai_usage.source == "push":
        return PushAIUsageAdapter(ai_usage, general, env)
    return FixtureAIUsageAdapter(ai_usage, general, env)
