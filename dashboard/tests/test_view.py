"""View helpers: unknown handling, task ordering, accents."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any

import pytest

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
    ai_capacity_rows,
    build_context,
    due_label,
    header_weather,
    meter_cells,
    percent_accent,
    priority_tasks,
    scale_position,
    sensor_value,
    task_accent,
    task_sort_key,
    today_scale,
    upcoming_events,
    wifi_level,
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
    assert today["header"]["weather"]["available"] is False
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


def test_ai_capacity_rows_show_unavailable_when_collection_failed() -> None:
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
    rows = ai_capacity_rows(state)
    claude, codex = rows[0]["windows"][0], rows[1]["windows"][0]
    # No percent sign: the numeral is the whole message, the "5H" label beside
    # it says what it is measuring.
    assert claude["value"] == "8"
    assert claude["accent"] == "red"
    assert claude["available"] is True
    # A failed collection is never displayed as a number; the hatch flag
    # takes its place instead.
    assert codex["value"] is None
    assert codex["available"] is False


def test_ai_capacity_rows_meter_fraction_is_the_used_share() -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(
                    provider="Claude",
                    short_window_percent_remaining=72,
                    weekly_percent_remaining=48,
                    collection_status="ok",
                )
            ],
        ),
    )
    windows = ai_capacity_rows(state)[0]["windows"]
    assert windows[0]["fraction"] == pytest.approx(0.28)
    assert windows[1]["fraction"] == pytest.approx(0.52)


def test_percent_accents_never_go_green() -> None:
    """Plenty of quota left is not news, and colouring the healthy case
    teaches the eye to ignore colour."""
    assert percent_accent(90, True) == "black"
    assert percent_accent(30, True) == "yellow"
    assert percent_accent(5, True) == "red"
    assert percent_accent(None, True) == "black"
    assert percent_accent(90, False) == "black"


def test_header_weather_without_data_is_the_hatch_flag() -> None:
    state = empty_state()
    assert header_weather(state) == {"available": False}


def test_header_weather_marks_a_stale_block_as_unavailable() -> None:
    state = empty_state()
    state.weather = WeatherBlock(status=AdapterStatus.STALE, weather=None)
    assert header_weather(state)["available"] is False


def test_header_weather_rain_gives_blue_and_the_rain_label() -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        weather=WeatherBlock(
            status=AdapterStatus.OK,
            weather=Weather(condition="Showers", temperature_c=29.0, rain_from="15:00"),
        ),
    )
    reading = header_weather(state)
    assert reading["available"] is True
    assert reading["color"] == "blue"
    assert reading["dot"] == "blue"
    assert reading["label"] == "RAIN 15:00"
    assert reading["temp"] == "29"


def test_header_weather_heat_outranks_rain_and_gives_red() -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        weather=WeatherBlock(
            status=AdapterStatus.OK,
            weather=Weather(
                condition="Sunny", temperature_c=37.0, rain_from="15:00"
            ),
        ),
    )
    reading = header_weather(state)
    assert reading["color"] == "red"
    assert reading["dot"] == "red"
    assert reading["label"] == "HEAT"


def test_header_weather_plain_condition_has_no_dot() -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        weather=WeatherBlock(
            status=AdapterStatus.OK,
            weather=Weather(condition="Cloudy", temperature_c=28.0),
        ),
    )
    reading = header_weather(state)
    assert reading["color"] == ""
    assert reading["dot"] == ""
    assert reading["label"] == "CLOUDY"


def test_wifi_level_thresholds() -> None:
    assert wifi_level(None) == "off"
    assert wifi_level(-90) == "off"
    assert wifi_level(-81) == "off"
    assert wifi_level(-80) == "low"
    assert wifi_level(-68) == "low"
    assert wifi_level(-67) == "strong"
    assert wifi_level(-20) == "strong"


def test_scale_position_endpoints_and_a_mid_afternoon_time() -> None:
    assert scale_position(6 * 60) == 0.0
    assert scale_position(24 * 60) == 100.0
    assert scale_position(15 * 60 + 6) == pytest.approx(50.6, abs=0.05)
    # Outside the 06:00-24:00 span clamps rather than going negative or past 100.
    assert scale_position(1 * 60) == 0.0
    assert scale_position(25 * 60) == 100.0


def test_today_scale_drops_a_close_marker_to_the_next_line() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    close_events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="a", title="First", start=datetime(2026, 9, 4, 12, 0, tzinfo=tz)),
            # Four minutes later lands well under the 12 px collision distance.
            Event(id="b", title="Second", start=datetime(2026, 9, 4, 12, 4, tzinfo=tz)),
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=close_events)
    markers = today_scale(state, {}, today, reference)["markers"]
    assert markers[0]["row"] == 0
    assert markers[1]["row"] == 1


def test_today_scale_keeps_well_spaced_markers_on_one_line() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    # Before either event starts, so both are still "next" and get a marker.
    reference = datetime(2026, 9, 4, 7, 0, tzinfo=tz)
    spaced_events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="a", title="Standup", start=datetime(2026, 9, 4, 8, 0, tzinfo=tz)),
            Event(id="b", title="Retro", start=datetime(2026, 9, 4, 20, 0, tzinfo=tz)),
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=spaced_events)
    markers = today_scale(state, {}, today, reference)["markers"]
    assert len(markers) == 2
    assert markers[0]["row"] == 0
    assert markers[1]["row"] == 0


def test_today_scale_excludes_events_before_six_am() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    early = CalendarBlock(
        status=AdapterStatus.OK,
        items=[Event(id="a", title="Early flight", start=datetime(2026, 9, 4, 5, 0, tzinfo=tz))],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=early)
    assert today_scale(state, {}, today, reference)["markers"] == []


def test_today_scale_drops_events_that_already_ended() -> None:
    """The scale looks forward: a meeting that is over is not still "now"."""
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 15, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(
                id="a",
                title="Standup",
                start=datetime(2026, 9, 4, 9, 30, tzinfo=tz),
                end=datetime(2026, 9, 4, 9, 45, tzinfo=tz),
            ),
            Event(id="b", title="Demo", start=datetime(2026, 9, 4, 16, 30, tzinfo=tz)),
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    markers = today_scale(state, {}, today, reference)["markers"]
    assert [marker["title"] for marker in markers] == ["Demo"]


def test_due_chip_accents() -> None:
    """Overdue is red, due today is yellow, anything else is plain black."""
    assert task_accent(Task(id="1", title="x", due=TODAY - timedelta(days=1)), TODAY) == "red"
    assert task_accent(Task(id="2", title="x", due=TODAY), TODAY) == "yellow"
    assert task_accent(Task(id="3", title="x", due=TODAY + timedelta(days=1)), TODAY) == "black"
    assert task_accent(Task(id="4", title="x", due=None), TODAY) == "black"


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


def test_every_page_carries_the_header_and_the_window_list(settings: Settings) -> None:
    state = empty_state()
    for page in ("today", "agenda", "weather", "brief", "system", "alert"):
        context = build_context(page, state, settings)
        windows = context["footer"]["windows"]
        assert [window["name"] for window in windows] == [
            "TODAY",
            "AGENDA",
            "WEATHER",
            "BRIEF",
            "SYSTEM",
        ]
        # The alert page interrupts, so no window entry is the active one.
        active = [window["name"] for window in windows if window["active"]]
        assert active == ([] if page == "alert" else [page.upper()])
        # Nothing to report is not the same as reporting zero.
        assert context["header"]["overdue_count"] == 0
        assert context["header"]["weather"]["available"] is False
        assert context["header"]["battery"]["chip_accent"] == ""


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
    assert context["header"]["overdue_count"] == 2


def _flagged(state: DashboardState, settings: Settings) -> set[str]:
    return {
        window["name"]
        for window in build_context("today", state, settings)["footer"]["windows"]
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
    assert not any(window["flag"] for window in context["footer"]["windows"])


def test_worst_accent_takes_the_loudest() -> None:
    assert worst_accent(["green", "red", "yellow"]) == "red"
    assert worst_accent(["green", "yellow"]) == "yellow"
    assert worst_accent(["green", "black"]) == "green"
    assert worst_accent(["black"]) == "black"
    assert worst_accent([]) == "black"


def test_today_has_no_title_accents_left_to_carry(settings: Settings) -> None:
    """Today refuses the pane title bar entirely: nothing fills it any more."""
    state = empty_state()
    assert build_context("today", state, settings)["title_accents"] == {}


def test_other_pages_still_carry_their_pane_title_accents(settings: Settings) -> None:
    """Agenda, weather, brief and system keep the older chrome for now."""
    state = empty_state()
    assert build_context("agenda", state, settings)["title_accents"] == {
        "days": "black",
        "then": "black",
    }
    assert build_context("weather", state, settings)["title_accents"] == {"weather": "black"}


def test_ai_capacity_accent_never_goes_green(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
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
    windows = context["providers"][0]["windows"]
    assert windows[0]["accent"] == "black"
    assert windows[1]["accent"] == "red"


def test_header_carries_the_overdue_flag_and_a_neutral_battery(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[Task(id="1", title="Late", due=TODAY - timedelta(days=1))],
        ),
    )
    header = build_context("today", state, settings)["header"]
    assert header["day"] == "04"
    assert header["weekday"] == "FRI"
    assert header["month"] == "SEP"
    assert header["overdue_count"] == 1
    # No device at all: the battery reading is neutral, never a guessed chip.
    assert header["battery"]["chip_accent"] == ""


def test_sensor_value_formatting() -> None:
    assert sensor_value(None, None) == "unknown"
    assert sensor_value("Closed", None) == "Closed"
    assert sensor_value("64", "%") == "64%"
    assert sensor_value("27.8", "C") == "27.8 C"
