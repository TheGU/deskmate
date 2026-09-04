"""Timezone helpers. Everything the pages show is local to ``TIMEZONE``."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo


@lru_cache(maxsize=8)
def zone(timezone_name: str) -> ZoneInfo:
    return ZoneInfo(timezone_name)


def now_local(timezone_name: str) -> datetime:
    return datetime.now(tz=zone(timezone_name))


def today_local(timezone_name: str) -> date:
    return now_local(timezone_name).date()


def to_local(value: datetime, timezone_name: str) -> datetime:
    """Attach ``TIMEZONE`` to a naive datetime, or convert an aware one."""
    tz = zone(timezone_name)
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def shift_days(value: date, days: int) -> date:
    return value + timedelta(days=days)


def hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")
