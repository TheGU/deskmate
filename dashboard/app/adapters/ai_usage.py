"""AI quota adapters: fixtures and a plain JSON file another agent writes.

There is no supported public API for Claude or Codex quota, so the hub never
scrapes anything. A separate collector writes ``data/ai-usage.json``; if the
file is missing, stale or malformed the page prints "unknown". See
docs/DATA-SOURCES.md for the schema.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.adapters.base import AdapterUnavailable
from app.adapters.fixtures import day_delta, load_fixture, shift_tree
from app.config import Settings
from app.models import AIUsage
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

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self) -> list[AIUsage]:
        settings = self._settings
        payload = load_fixture(settings.fixtures_dir / "ai_usage.json")
        delta = day_delta(
            payload, today_local(settings.timezone), enabled=settings.fixture_relative_dates
        )
        raw: Any = shift_tree(payload.get("providers", []), delta, DATE_KEYS)
        providers = [AIUsage.model_validate(item) for item in raw]
        return _localize(providers, settings.timezone)


class FileAIUsageAdapter:
    """Quota from ``AI_USAGE_PATH`` (default ``data/ai-usage.json``)."""

    name = "ai_usage"
    source = "file"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self) -> list[AIUsage]:
        path = self._settings.ai_usage_file
        if not path.is_file():
            raise AdapterUnavailable(f"ai usage file not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            payload: Any = json.load(handle)
        raw: Any
        if isinstance(payload, dict):
            raw = payload.get("providers", [])
        elif isinstance(payload, list):
            raw = payload
        else:
            raise ValueError(f"{path} must contain an object or a list")
        providers = [AIUsage.model_validate(item) for item in raw]
        return _localize(providers, self._settings.timezone)


def build_ai_usage_adapter(settings: Settings) -> FixtureAIUsageAdapter | FileAIUsageAdapter:
    if settings.ai_usage_source == "file":
        return FileAIUsageAdapter(settings)
    return FixtureAIUsageAdapter(settings)
