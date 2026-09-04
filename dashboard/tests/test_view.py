"""View helpers: unknown handling, task ordering, accents."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.config import Settings
from app.models import (
    AdapterStatus,
    AIUsage,
    AIUsageBlock,
    CalendarBlock,
    DashboardState,
    Event,
    Priority,
    Task,
    TasksBlock,
    WeatherBlock,
)
from app.timeutil import zone
from app.view import (
    build_context,
    due_label,
    percent_accent,
    priority_tasks,
    sensor_value,
    task_sort_key,
    upcoming_events,
    usage_rows,
    weather_summary,
)

TODAY = date(2026, 9, 4)


def empty_state() -> DashboardState:
    """A state where every adapter failed. Pages must still render."""
    tz = zone("Asia/Bangkok")
    return DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
    )


def test_every_block_defaults_to_unavailable() -> None:
    state = empty_state()
    assert all(block.status is AdapterStatus.UNAVAILABLE for block in state.blocks.values())
    assert not any(block.usable for block in state.blocks.values())


def test_pages_render_context_when_everything_is_unavailable(settings: Settings) -> None:
    state = empty_state()
    for page in ("today", "agenda", "weather", "brief", "system", "alert"):
        context = build_context(page, state, settings)
        assert context["page"] == page
    today = build_context("today", state, settings)
    assert today["priorities"] == []
    assert today["weather_summary"]["detail"] == "unavailable"
    assert "unavailable" in today["ai_note"]["text"]
    weather = build_context("weather", state, settings)
    assert weather["weather"] is None
    brief = build_context("brief", state, settings)
    assert brief["sections"] == []


def test_task_order_puts_overdue_first_then_priority() -> None:
    tasks = [
        Task(id="a", title="Later high", due=TODAY + timedelta(days=3), priority=Priority.HIGH),
        Task(id="b", title="Overdue low", due=TODAY - timedelta(days=2), priority=Priority.LOW),
        Task(id="c", title="Today medium", due=TODAY, priority=Priority.MEDIUM),
    ]
    ordered = sorted(tasks, key=lambda task: task_sort_key(task, TODAY))
    assert [task.id for task in ordered] == ["b", "a", "c"]


def test_priority_tasks_skips_completed_and_respects_the_limit(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[
                Task(id="1", title="One", due=TODAY, priority=Priority.HIGH),
                Task(id="2", title="Two", due=TODAY, priority=Priority.HIGH),
                Task(id="3", title="Three", due=TODAY, priority=Priority.HIGH),
                Task(id="4", title="Four", due=TODAY, priority=Priority.HIGH),
                Task(id="5", title="Done", due=TODAY, completed=True),
            ],
        ),
    )
    rows = priority_tasks(state, TODAY, settings.max_priority_tasks)
    assert len(rows) == 3
    assert "Done" not in [row["title"] for row in rows]


def test_due_labels() -> None:
    assert due_label(None, TODAY) == ""
    assert due_label(TODAY, TODAY) == "TODAY"
    assert due_label(TODAY + timedelta(days=1), TODAY) == "TOMORROW"
    assert due_label(TODAY - timedelta(days=1), TODAY) == "1D LATE"
    assert due_label(TODAY - timedelta(days=3), TODAY) == "3D LATE"
    assert due_label(TODAY + timedelta(days=5), TODAY) == "DUE 09 SEP"


def test_upcoming_events_labels_and_skips_the_past() -> None:
    tz = zone("Asia/Bangkok")
    now = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    state = DashboardState(
        generated_at=now,
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(
            status=AdapterStatus.OK,
            items=[
                Event(id="past", title="Standup", start=datetime(2026, 9, 4, 9, 30, tzinfo=tz)),
                Event(id="later", title="Demo", start=datetime(2026, 9, 4, 16, 30, tzinfo=tz)),
                Event(id="next", title="Drill", start=datetime(2026, 9, 5, 18, 0, tzinfo=tz)),
                Event(
                    id="allday",
                    title="Offsite",
                    start=datetime(2026, 9, 10, 0, 0, tzinfo=tz),
                    all_day=True,
                ),
            ],
        ),
    )
    rows = upcoming_events(state, now, 5)
    assert [row["title"] for row in rows] == ["Demo", "Drill", "Offsite"]
    assert rows[0]["when"] == "16:30"
    assert rows[1]["when"] == "TOMORROW 18:00"
    assert rows[2]["when"] == "THU"


def test_usage_rows_show_unknown_when_collection_failed() -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(provider="Claude", short_window_percent_remaining=8, collection_status="ok"),
                AIUsage(
                    provider="Codex",
                    short_window_percent_remaining=99,
                    collection_status="error",
                ),
            ],
        ),
    )
    rows = usage_rows(state)
    assert rows[0]["short"] == "8%"
    assert rows[0]["short_accent"] == "red"
    # A failed collection is never displayed as a number.
    assert rows[1]["short"] == "unknown"


def test_percent_accents() -> None:
    assert percent_accent(90, True) == "green"
    assert percent_accent(30, True) == "yellow"
    assert percent_accent(5, True) == "red"
    assert percent_accent(None, True) == "black"
    assert percent_accent(90, False) == "black"


def test_weather_summary_without_data() -> None:
    state = empty_state()
    assert weather_summary(state) == {"temp": "--", "detail": "unavailable", "accent": "black"}


def test_weather_summary_marks_a_stale_block_as_usable() -> None:
    state = empty_state()
    state.weather = WeatherBlock(status=AdapterStatus.STALE, weather=None)
    # Stale with no value must still not invent a temperature.
    assert weather_summary(state)["temp"] == "--"


def test_sensor_value_formatting() -> None:
    assert sensor_value(None, None) == "unknown"
    assert sensor_value("Closed", None) == "Closed"
    assert sensor_value("64", "%") == "64%"
    assert sensor_value("27.8", "C") == "27.8 C"
