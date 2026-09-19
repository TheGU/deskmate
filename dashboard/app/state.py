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
from app.config import Env
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
from app.settings import HubSettings
from app.timeutil import now_local

logger = logging.getLogger("app.state")

BlockT = TypeVar("BlockT", bound=Block)


class StateService:
    """Owns the cached adapters and produces state snapshots."""

    def __init__(self, hub_settings: HubSettings, env: Env, alerts: AlertStore) -> None:
        self._hub_settings = hub_settings
        self._env = env
        self._alerts = alerts
        general = hub_settings.general
        self.tasks = CachedAdapter(
            build_tasks_adapter(hub_settings.tasks, general, env), hub_settings.tasks.ttl_seconds
        )
        self.calendar = CachedAdapter(
            build_calendar_adapter(hub_settings.calendar, general, env),
            hub_settings.calendar.ttl_seconds,
        )
        self.weather = CachedAdapter(
            build_weather_adapter(hub_settings.weather, general, env),
            hub_settings.weather.ttl_seconds,
        )
        self.ai_usage = CachedAdapter(
            build_ai_usage_adapter(hub_settings.ai_usage, general, env),
            hub_settings.ai_usage.ttl_seconds,
        )
        self.brief = CachedAdapter(
            build_brief_adapter(hub_settings.brief, general, env), hub_settings.brief.ttl_seconds
        )
        self.home = CachedAdapter(
            build_home_adapter(hub_settings.home, env), hub_settings.home.ttl_seconds
        )
        self.device = CachedAdapter(
            build_device_adapter(hub_settings.device, env), hub_settings.device.ttl_seconds
        )

    @property
    def adapters(self) -> dict[str, CachedAdapter[Any]]:
        """Every cached adapter, keyed and ordered like DashboardState.blocks.
        Each adapter's own ``name`` (adapters/*.py) already matches its key
        here, so this is just the explicit registry /healthz walks without
        forcing a fetch.
        """
        return {
            "tasks": self.tasks,
            "calendar": self.calendar,
            "weather": self.weather,
            "ai_usage": self.ai_usage,
            "brief": self.brief,
            "home": self.home,
            "device": self.device,
        }

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
            generated_at=now_local(self._hub_settings.general.timezone),
            timezone=self._hub_settings.general.timezone,
            blocks={
                "tasks": build_block(TasksBlock, tasks_out, "items"),
                "calendar": build_block(CalendarBlock, calendar_out, "items"),
                "weather": build_block(WeatherBlock, weather_out, "weather"),
                "ai_usage": build_block(AIUsageBlock, usage_out, "providers"),
                "brief": build_block(BriefBlock, brief_out, "brief"),
                "home": build_block(HomeBlock, home_out, "home"),
                "device": build_block(DeviceBlock, device_out, "device"),
            },
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


def build_block(model: type[BlockT], outcome: Outcome[Any], value_field: str) -> BlockT:
    """One adapter outcome as its block: the envelope plus the one field the
    block type carries the value in.

    An outcome with no value leaves ``value_field`` off entirely rather than
    passing an empty sentinel, so the block type's own default (an empty
    list, or ``None``) is what an unavailable dataset shows. That is the
    same result the explicit ``empty`` argument produced before 2.1a, minus
    a second place to keep the two in step.
    """
    extra: dict[str, Any] = {} if outcome.value is None else {value_field: outcome.value}
    return model(
        status=outcome.status,
        source=outcome.source,
        updated_at=outcome.updated_at,
        error=outcome.error,
        # Only AIUsageBlock, BriefBlock and TasksBlock declare this field;
        # pydantic's default extra="ignore" drops it for the other blocks.
        received_at=outcome.received_at,
        **extra,
    )


def state_fingerprint(state: DashboardState) -> str:
    """Stable hash of everything a page can render.

    ``generated_at`` is excluded on purpose: it moves on every request, while
    the rendered pixels only change when an adapter produced something new.
    """
    payload = state.model_dump_json(exclude={"generated_at"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["StateService", "build_block", "state_fingerprint"]
