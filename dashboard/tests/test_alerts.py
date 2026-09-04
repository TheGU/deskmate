"""Alert priority rules and persistence."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.alerts import AlertStore
from app.models import ALERT_PRIORITY_RANK, AlertPriority, AlertRequest

TIMEZONE = "Asia/Bangkok"


@pytest.fixture()
def store(tmp_path: Path) -> AlertStore:
    return AlertStore(tmp_path / "alert.json", TIMEZONE)


def request(priority: str, title: str = "Test", duration: int = 90) -> AlertRequest:
    return AlertRequest(title=title, priority=AlertPriority(priority), duration_seconds=duration)


def test_priority_order_matches_the_spec() -> None:
    ranks = ALERT_PRIORITY_RANK
    assert (
        ranks[AlertPriority.CRITICAL]
        > ranks[AlertPriority.DOORBELL]
        > ranks[AlertPriority.IMPORTANT]
        > ranks[AlertPriority.NORMAL]
    )


def test_first_alert_is_always_accepted(store: AlertStore) -> None:
    alert, accepted = store.set(request("normal"))
    assert accepted is True
    assert store.current is not None
    assert store.current.title == alert.title


def test_higher_priority_replaces_an_active_alert(store: AlertStore) -> None:
    store.set(request("normal", "Laundry"))
    alert, accepted = store.set(request("critical", "Smoke"))
    assert accepted is True
    assert alert.title == "Smoke"


def test_equal_priority_replaces_an_active_alert(store: AlertStore) -> None:
    store.set(request("doorbell", "First ring"))
    alert, accepted = store.set(request("doorbell", "Second ring"))
    assert accepted is True
    assert alert.title == "Second ring"


def test_lower_priority_does_not_replace_an_active_alert(store: AlertStore) -> None:
    store.set(request("critical", "Smoke"))
    alert, accepted = store.set(request("normal", "Laundry"))
    assert accepted is False
    assert alert.title == "Smoke"
    assert store.current is not None
    assert store.current.title == "Smoke"


def test_lower_priority_replaces_an_expired_alert(store: AlertStore) -> None:
    store.set(request("critical", "Smoke", duration=5))
    assert store.current is not None
    store.current.created_at = store.current.created_at - timedelta(seconds=60)
    alert, accepted = store.set(request("normal", "Laundry"))
    assert accepted is True
    assert alert.title == "Laundry"


def test_clear_removes_the_alert_and_the_file(tmp_path: Path) -> None:
    path = tmp_path / "alert.json"
    store = AlertStore(path, TIMEZONE)
    store.set(request("doorbell"))
    assert path.is_file()
    assert store.clear() is True
    assert store.current is None
    assert not path.exists()
    assert store.clear() is False


def test_alert_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "alert.json"
    first = AlertStore(path, TIMEZONE)
    first.set(request("doorbell", "Someone is at the door"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["priority"] == "doorbell"

    second = AlertStore(path, TIMEZONE)
    assert second.current is not None
    assert second.current.title == "Someone is at the door"


def test_a_corrupt_alert_file_does_not_break_startup(tmp_path: Path) -> None:
    path = tmp_path / "alert.json"
    path.write_text("{ not json", encoding="utf-8")
    store = AlertStore(path, TIMEZONE)
    assert store.current is None


def test_duration_bounds_are_validated() -> None:
    with pytest.raises(ValueError):
        AlertRequest(title="x", duration_seconds=1)
    with pytest.raises(ValueError):
        AlertRequest(title="x", duration_seconds=10_000)
