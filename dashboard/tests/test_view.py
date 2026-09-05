"""View helpers: unknown handling, task ordering, accents."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any

from app.config import Settings
from app.models import (
    AdapterStatus,
    AIUsage,
    AIUsageBlock,
    CalendarBlock,
    DashboardState,
    DeviceBlock,
    DeviceState,
    DeviceStatus,
    Event,
    HomeBlock,
    HomeState,
    Priority,
    ServiceHealth,
    ServiceStatus,
    Task,
    TasksBlock,
    Weather,
    WeatherBlock,
)
from app.timeutil import zone
from app.view import (
    build_context,
    due_label,
    meter_cells,
    percent_accent,
    priority_tasks,
    sensor_value,
    task_sort_key,
    upcoming_events,
    usage_rows,
    weather_summary,
    worst_accent,
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


def test_percent_accents_never_go_green() -> None:
    """Plenty of quota left is not news, and colouring the healthy case
    teaches the eye to ignore colour."""
    assert percent_accent(90, True) == "black"
    assert percent_accent(30, True) == "yellow"
    assert percent_accent(5, True) == "red"
    assert percent_accent(None, True) == "black"
    assert percent_accent(90, False) == "black"


def test_weather_summary_without_data() -> None:
    state = empty_state()
    summary = weather_summary(state)
    assert summary["temp"] == "--"
    assert summary["detail"] == "unavailable"
    assert summary["accent"] == "black"
    # The status bar segment says so too, rather than printing a bare "--".
    assert summary["short"] == "WEATHER UNAVAILABLE"


def test_weather_summary_marks_a_stale_block_as_usable() -> None:
    state = empty_state()
    state.weather = WeatherBlock(status=AdapterStatus.STALE, weather=None)
    # Stale with no value must still not invent a temperature.
    assert weather_summary(state)["temp"] == "--"


def test_meter_cells_round_to_ten_blocks() -> None:
    assert sum(meter_cells(100)) == 10
    assert sum(meter_cells(72)) == 7
    assert sum(meter_cells(75)) == 8
    assert sum(meter_cells(0)) == 0
    assert len(meter_cells(48)) == 10
    # Unknown is ten empty cells, never a guessed fill.
    assert sum(meter_cells(None)) == 0
    assert sum(meter_cells(90, filled=False)) == 0
    # A value outside 0..100 still fills a whole meter, never more.
    assert sum(meter_cells(140)) == 10
    assert sum(meter_cells(-5)) == 0


def test_every_page_carries_the_status_bar_and_the_window_list(settings: Settings) -> None:
    state = empty_state()
    for page in ("today", "agenda", "weather", "brief", "system", "alert"):
        context = build_context(page, state, settings)
        assert context["page_name"]
        assert context["page_icon"]
        assert [window["name"] for window in context["windows"]] == [
            "TODAY",
            "AGENDA",
            "WEATHER",
            "BRIEF",
            "SYSTEM",
        ]
        # The alert page interrupts, so no window entry is the active one.
        active = [window["name"] for window in context["windows"] if window["active"]]
        assert active == ([] if page == "alert" else [context["page_name"]])
        # Nothing to report is not the same as reporting zero.
        assert context["overdue_label"] == ""
        assert context["device_badge"]["available"] is False


def test_overdue_segment_counts_only_open_late_tasks(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[
                Task(id="1", title="Late", due=TODAY - timedelta(days=1)),
                Task(id="2", title="Later", due=TODAY - timedelta(days=4)),
                Task(id="3", title="Due today", due=TODAY),
                Task(id="4", title="Late but done", due=TODAY - timedelta(days=2), completed=True),
            ],
        ),
    )
    context = build_context("weather", state, settings)
    assert context["overdue_count"] == 2
    assert context["overdue_label"] == "2 LATE"


def _flagged(state: DashboardState, settings: Settings) -> set[str]:
    return {
        window["name"]
        for window in build_context("today", state, settings)["windows"]
        if window["flag"]
    }


def _flag_state(**blocks: Any) -> DashboardState:
    tz = zone("Asia/Bangkok")
    return DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        **blocks,
    )


def test_window_list_flags_the_pages_that_need_attention(settings: Settings) -> None:
    state = _flag_state(
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[Task(id="1", title="Late", due=TODAY - timedelta(days=1))],
        ),
        # Red air, not rain: the status bar already reports rain everywhere.
        weather=WeatherBlock(
            status=AdapterStatus.OK,
            weather=Weather(condition="Hazy", uv_index=9.1, rain_probability_percent=10),
        ),
        home=HomeBlock(
            status=AdapterStatus.OK,
            home=HomeState(
                services=[ServiceStatus(key="nas", name="NAS", health=ServiceHealth.DOWN)]
            ),
        ),
    )
    assert _flagged(state, settings) == {"AGENDA", "WEATHER", "SYSTEM"}


def test_window_list_flags_are_selective(settings: Settings) -> None:
    """A degraded service, or a wet afternoon, is not worth a flag."""
    state = _flag_state(
        weather=WeatherBlock(
            status=AdapterStatus.OK,
            weather=Weather(
                condition="Showers",
                rain_probability_percent=95,
                uv_index=2.0,
                pm2_5=8.0,
                aqi=20,
            ),
        ),
        home=HomeBlock(
            status=AdapterStatus.OK,
            home=HomeState(
                services=[ServiceStatus(key="nas", name="NAS", health=ServiceHealth.WARN)]
            ),
        ),
    )
    assert _flagged(state, settings) == set()


def test_window_list_flags_system_when_the_paper_goes_quiet(settings: Settings) -> None:
    now = datetime(2026, 9, 4, 12, 0, tzinfo=dt_timezone.utc)
    stale = DeviceState(
        status=DeviceStatus.STALE,
        device="reterminal-e1002",
        received_at=now,
        age_seconds=7200.0,
        temperature=30.0,
    )
    state = _flag_state(device=DeviceBlock(status=AdapterStatus.OK, source="store", device=stale))
    assert _flagged(state, settings) == {"SYSTEM"}


def test_window_list_has_no_flags_when_nothing_needs_attention(settings: Settings) -> None:
    state = _flag_state(
        weather=WeatherBlock(
            status=AdapterStatus.OK,
            weather=Weather(condition="Sunny", uv_index=3.0, pm2_5=6.0, aqi=18),
        ),
        home=HomeBlock(
            status=AdapterStatus.OK,
            home=HomeState(
                services=[ServiceStatus(key="net", name="Internet", health=ServiceHealth.OK)]
            ),
        ),
    )
    context = build_context("today", state, settings)
    assert not any(window["flag"] for window in context["windows"])


def test_worst_accent_takes_the_loudest() -> None:
    assert worst_accent(["green", "red", "yellow"]) == "red"
    assert worst_accent(["green", "yellow"]) == "yellow"
    assert worst_accent(["green", "black"]) == "green"
    assert worst_accent(["black"]) == "black"
    assert worst_accent([]) == "black"


def test_title_accents_carry_the_meaning_of_each_pane(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[Task(id="1", title="Late", due=TODAY - timedelta(days=1))],
        ),
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(
                    provider="Claude",
                    short_window_percent_remaining=90,
                    weekly_percent_remaining=8,
                    collection_status="ok",
                )
            ],
        ),
    )
    context = build_context("today", state, settings)
    # Every Today bar is neutral: the red flag in the band, the red due chip
    # and the red percentage carry the state instead.
    assert context["title_accents"] == {
        "priorities": "black",
        "next": "black",
        "capacity": "black",
        "note": "black",
    }
    assert context["providers"][0]["weekly_accent"] == "red"
    assert context["providers"][0]["short_accent"] == "black"


def test_status_band_entries_are_neutral_unless_they_report_something(
    settings: Settings,
) -> None:
    """White band, black type. Only the page block and a state are filled."""
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[Task(id="1", title="Late", due=TODAY - timedelta(days=1))],
        ),
    )
    context = build_context("today", state, settings)
    left = context["left_entries"]
    assert [item["text"] for item in left][:2] == ["FRI 04 SEP", "TODAY"]
    # The date is plain paper; the page name is the inverted block.
    assert left[0]["field"] == "" and left[0]["inverted"] is False
    assert left[1]["inverted"] is True

    right = context["right_entries"]
    # Overdue is a state and fills red; the clock is not and stays neutral.
    assert right[0]["field"] == "red"
    assert right[-1]["field"] == ""


def test_battery_entry_is_neutral_until_it_is_worth_saying(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz), timezone="Asia/Bangkok"
    )
    # No device at all: no battery entry, and the clock stays neutral.
    assert build_context("today", state, settings)["right_entries"][-1]["field"] == ""


def test_context_segments_are_page_specific(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz), timezone="Asia/Bangkok"
    )
    def texts(page: str) -> list[str]:
        entries = build_context(page, state, settings)["context_entries"]
        return [item["text"] for item in entries]

    # An unavailable adapter still produces an entry, and it says so.
    assert texts("today") == ["WEATHER UNAVAILABLE"]
    # No calendars in play, so the legend falls back to the window length.
    assert texts("agenda") == ["7 DAYS"]
    assert texts("system") == ["HOME"]
    # Alert has no context entry: the page entry already carries the accent.
    assert texts("alert") == []
    # No alert, so nothing to report: the page entry stays the plain block.
    assert build_context("alert", state, settings)["page_field"] == ""


def test_sensor_value_formatting() -> None:
    assert sensor_value(None, None) == "unknown"
    assert sensor_value("Closed", None) == "Closed"
    assert sensor_value("64", "%") == "64%"
    assert sensor_value("27.8", "C") == "27.8 C"
