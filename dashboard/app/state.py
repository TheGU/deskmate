"""Assembles the normalized :class:`DashboardState` from every adapter.

One failing adapter never breaks a page: each block carries its own status and
the templates print "unknown" / "unavailable" instead of a made up value.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, TypeVar

from app.adapters.ai_brief import build_brief_adapter
from app.adapters.ai_usage import build_ai_usage_adapter
from app.adapters.base import CachedAdapter, Outcome
from app.adapters.calendar import build_calendar_adapter
from app.adapters.device import build_device_adapter
from app.adapters.home_assistant import build_home_adapter
from app.adapters.tasks import build_tasks_adapter
from app.adapters.weather import build_weather_adapter
from app.alerts import AlertStore
from app.config import Settings
from app.logging_setup import log
from app.models import (
    AIUsageBlock,
    Block,
    BriefBlock,
    CalendarBlock,
    DashboardState,
    DeviceBlock,
    HomeBlock,
    TasksBlock,
    WeatherBlock,
)
from app.timeutil import now_local

logger = logging.getLogger("app.state")

BlockT = TypeVar("BlockT", bound=Block)


class StateService:
    """Owns the cached adapters and produces state snapshots."""

    def __init__(self, settings: Settings, alerts: AlertStore) -> None:
        self._settings = settings
        self._alerts = alerts
        self.tasks = CachedAdapter(build_tasks_adapter(settings), settings.tasks_ttl_seconds)
        self.calendar = CachedAdapter(
            build_calendar_adapter(settings), settings.calendar_ttl_seconds
        )
        self.weather = CachedAdapter(build_weather_adapter(settings), settings.weather_ttl_seconds)
        self.ai_usage = CachedAdapter(
            build_ai_usage_adapter(settings), settings.ai_usage_ttl_seconds
        )
        self.brief = CachedAdapter(build_brief_adapter(settings), settings.brief_ttl_seconds)
        self.home = CachedAdapter(build_home_adapter(settings), settings.home_ttl_seconds)
        self.device = CachedAdapter(build_device_adapter(settings), settings.device_ttl_seconds)

    async def build(self, *, force: bool = False) -> DashboardState:
        """Fetch every adapter (concurrently) and fold the results into state."""
        (
            tasks_out,
            calendar_out,
            weather_out,
            usage_out,
            brief_out,
            home_out,
            device_out,
        ) = await asyncio.gather(
            self.tasks.get(force=force),
            self.calendar.get(force=force),
            self.weather.get(force=force),
            self.ai_usage.get(force=force),
            self.brief.get(force=force),
            self.home.get(force=force),
            self.device.get(force=force),
        )

        state = DashboardState(
            generated_at=now_local(self._settings.timezone),
            timezone=self._settings.timezone,
            tasks=_block(TasksBlock, tasks_out, "items", []),
            calendar=_block(CalendarBlock, calendar_out, "items", []),
            weather=_block(WeatherBlock, weather_out, "weather", None),
            ai_usage=_block(AIUsageBlock, usage_out, "providers", []),
            brief=_block(BriefBlock, brief_out, "brief", None),
            home=_block(HomeBlock, home_out, "home", None),
            device=_block(DeviceBlock, device_out, "device", None),
            alert=self._alerts.current,
        )
        log(
            logger,
            logging.INFO,
            "state built",
            forced=force,
            **{name: block.status.value for name, block in state.blocks.items()},
        )
        return state


def _block(model: type[BlockT], outcome: Outcome[Any], field: str, empty: Any) -> BlockT:
    value = outcome.value if outcome.value is not None else empty
    return model(
        status=outcome.status,
        source=outcome.source,
        updated_at=outcome.updated_at,
        error=outcome.error,
        # Only AIUsageBlock, BriefBlock and TasksBlock declare this field;
        # pydantic's default extra="ignore" drops it for the other blocks.
        received_at=outcome.received_at,
        **{field: value},
    )


def state_fingerprint(state: DashboardState) -> str:
    """Stable hash of everything a page can render.

    ``generated_at`` is excluded on purpose: it moves on every request, while
    the rendered pixels only change when an adapter produced something new.
    """
    payload = state.model_dump_json(exclude={"generated_at"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["StateService", "state_fingerprint"]
