"""AI quota adapters: fixtures and a plain JSON file another agent writes.

There is no supported public API for Claude or Codex quota, so the hub never
scrapes anything. A separate collector writes ``data/ai-usage.json``; if the
file is missing, stale or malformed the page prints "unknown". See
docs/DATA-SOURCES.md for the schema.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.adapters.base import AdapterUnavailable, received_at_or_mtime
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
    """Quota from ``AI_USAGE_PATH`` (default ``data/ai-usage.json``), the
    shape ``POST /api/ai-usage`` writes."""

    name = "ai_usage"
    source = "file"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        #: Set on every successful fetch: the file's own ``received_at``, or
        #: its mtime. Read by ``CachedAdapter`` for ``AIUsageBlock.received_at``.
        self.last_received_at: datetime | None = None

    async def fetch(self) -> list[AIUsage]:
        path = self._settings.ai_usage_file
        if not path.is_file():
            raise AdapterUnavailable(f"ai usage file not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            payload: Any = json.load(handle)
        raw: Any
        received_raw: Any = None
        if isinstance(payload, dict):
            raw = payload.get("providers", [])
            received_raw = payload.get("received_at")
        elif isinstance(payload, list):
            raw = payload
        else:
            raise ValueError(f"{path} must contain an object or a list")
        providers = [AIUsage.model_validate(item) for item in raw]
        self.last_received_at = received_at_or_mtime(received_raw, path, self._settings.timezone)
        return _localize(providers, self._settings.timezone)


class AutoAIUsageAdapter:
    """"auto": the file adapter when its file exists, else fixture.

    Re-checked on every ``fetch()``, not just at startup, so a push made
    while the process is running switches the effective source once the
    endpoint calls ``invalidate()``. The file delegate is also given a
    chance whenever its file merely looks present: if it raises
    ``AdapterUnavailable``, or the file vanishes between this adapter's own
    ``is_file()`` check and the delegate's own read (TOCTOU), this falls
    back to fixture instead of surfacing an ``error`` block.
    """

    name = "ai_usage"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._file = FileAIUsageAdapter(settings)
        self._fixture = FixtureAIUsageAdapter(settings)
        #: Mirrors whichever delegate last ran, for ``CachedAdapter``.
        self.last_received_at: datetime | None = None
        #: The delegate actually used on the last fetch, set only inside
        #: fetch(). ``source`` reports this, not a fresh stat, so it keeps
        #: saying "file" between fetches even if the file is later deleted
        #: (it stays correct until the TTL or an invalidate() triggers the
        #: next real fetch).
        self._last_source = "fixture"

    @property
    def source(self) -> str:
        return self._last_source

    async def fetch(self) -> list[AIUsage]:
        if self._settings.ai_usage_file.is_file():
            try:
                value = await self._file.fetch()
            except (AdapterUnavailable, OSError):
                pass
            else:
                self._last_source = "file"
                self.last_received_at = getattr(self._file, "last_received_at", None)
                return value
        value = await self._fixture.fetch()
        self._last_source = "fixture"
        self.last_received_at = getattr(self._fixture, "last_received_at", None)
        return value


def build_ai_usage_adapter(
    settings: Settings,
) -> FixtureAIUsageAdapter | FileAIUsageAdapter | AutoAIUsageAdapter:
    if settings.ai_usage_source == "file":
        return FileAIUsageAdapter(settings)
    if settings.ai_usage_source == "auto":
        return AutoAIUsageAdapter(settings)
    return FixtureAIUsageAdapter(settings)
