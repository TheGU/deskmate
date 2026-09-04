"""Fixture loading with relative-safe dates.

Every fixture file carries an ``anchor_date``. When ``FIXTURE_RELATIVE_DATES``
is on (the default) the loader shifts every date and datetime in the file by
``today - anchor_date`` whole days, so the demo always looks like today no
matter when it is opened. Clock times are never changed, only the day.

Set ``FIXTURE_RELATIVE_DATES=false`` to read the literal dates in the files,
which is what the deterministic rendering tests want.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.adapters.base import AdapterUnavailable


def load_fixture(path: Path) -> dict[str, Any]:
    """Read one fixture JSON file."""
    if not path.is_file():
        raise AdapterUnavailable(f"fixture not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"fixture {path} must contain a JSON object")
    return payload


def day_delta(payload: dict[str, Any], today: date, *, enabled: bool) -> int:
    """Whole-day offset between the fixture anchor and ``today``."""
    if not enabled:
        return 0
    anchor = payload.get("anchor_date")
    if not isinstance(anchor, str):
        return 0
    return (today - date.fromisoformat(anchor)).days


def shift_iso(value: str | None, days: int) -> str | None:
    """Shift an ISO date or datetime string by ``days``, keeping the time."""
    if value is None:
        return None
    if days == 0:
        return value
    if len(value) == 10:
        return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
    return (datetime.fromisoformat(value) + timedelta(days=days)).isoformat()


def shift_tree(node: Any, days: int, keys: frozenset[str]) -> Any:
    """Recursively shift the ISO values found under ``keys``."""
    if days == 0:
        return node
    if isinstance(node, dict):
        return {
            key: shift_iso(value, days)
            if key in keys and isinstance(value, str)
            else shift_tree(value, days, keys)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [shift_tree(item, days, keys) for item in node]
    return node
