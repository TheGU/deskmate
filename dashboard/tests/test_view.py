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
    Alert,
    AlertPriority,
    Brief,
    BriefBlock,
    BriefMode,
    BriefSection,
    CalendarBlock,
    DailyForecast,
    DashboardState,
    DeviceBlock,
    DeviceState,
    DeviceStatus,
    Event,
    HomeBlock,
    HomeSensor,
    HomeState,
    HourlyRain,
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
    ALERT_BAND_ACCENT,
    AGENDA_LIST_ROW_LIMIT,
    BRIEF_CHIP_WIDTH_PX,
    BRIEF_TITLE_MAX_CHARS,
    PAGES_WITH_OWN_OVERDUE_CHIP,
    PRIORITY_TITLE_MAX_CHARS,
    TODAY_CHIP_WIDTH_PX,
    agenda_context,
    agenda_list_rows,
    ai_capacity_rows,
    ai_usage_stale,
    alert_context,
    battery_accent,
    block_note,
    brief_context,
    brief_due_label,
    brief_headline_fits_one_line,
    brief_is_risk,
    brief_lines,
    brief_stale,
    brief_task_rows,
    brief_title_budget,
    build_context,
    cap_duration,
    clip_words,
    dataset_age_label,
    desk_accent,
    device_panel,
    due_chip_kind,
    due_label,
    footer_context,
    header_context,
    header_weather,
    HOME_ROW_BUDGET,
    hub_rows,
    meter_cells,
    month_grid,
    next_seven_days,
    page_shows_demo_data,
    percent_accent,
    power_label,
    priority_tasks,
    priority_title_budget,
    sensor_accent,
    sensor_value,
    service_mark,
    stale_info,
    strip_scheme,
    system_context,
    task_accent,
    task_sort_key,
    tasks_stale,
    today_context,
    today_next_rows,
    today_priority_tasks,
    upcoming_events,
    wake_label,
    weather_daily_rows,
    weather_hourly_plates,
    weather_readings,
    weather_summary,
    wifi_accent,
    wifi_level,
    window_flags,
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
    assert weather["hero"]["available"] is False
    brief = build_context("brief", state, settings)
    assert brief["brief_available"] is False
    assert brief["lines"] == []
    system = build_context("system", state, settings)
    assert system["device"]["available"] is False
    assert system["home_rows"] == []
    assert len(system["hub"]) == 9
    assert {row["name"] for row in system["hub"]} == {
        "TASKS",
        "CALENDAR",
        "WEATHER",
        "AI USAGE",
        "BRIEF",
        "HOME",
        "DEVICE SYNC",
        "DEVICE IP",
        "HUB URL",
    }
    assert all(row["value"] == "NEVER" for row in system["hub"][:7])
    assert system["hub"][7]["available"] is False  # DEVICE IP: hatch
    assert system["hub"][8]["available"] is False  # HUB URL: hatch


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


def test_due_labels_use_weekday_not_tomorrow() -> None:
    """The word TOMORROW never appears: a task due tomorrow prints its
    3-letter weekday instead. due_label is the only due label now, used by
    both Today and Brief (through :func:`brief_due_label`)."""
    assert due_label(None, TODAY) == ""
    assert due_label(TODAY, TODAY) == "TODAY"
    tomorrow = TODAY + timedelta(days=1)
    assert due_label(tomorrow, TODAY) == tomorrow.strftime("%a").upper() == "SAT"
    assert "TOMORROW" not in due_label(tomorrow, TODAY)
    assert due_label(TODAY - timedelta(days=1), TODAY) == "1D LATE"
    assert due_label(TODAY - timedelta(days=3), TODAY) == "3D LATE"
    assert due_label(TODAY + timedelta(days=5), TODAY) == "DUE 09 SEP"


def test_brief_due_label_drops_the_due_word() -> None:
    """Brief's due column is a fixed 84 px with no room for the "DUE " word;
    every other case matches due_label exactly."""
    assert brief_due_label(None, TODAY) == ""
    assert brief_due_label(TODAY, TODAY) == "TODAY"
    assert brief_due_label(TODAY - timedelta(days=1), TODAY) == "1D LATE"
    assert brief_due_label(TODAY + timedelta(days=1), TODAY) == "SAT"
    assert brief_due_label(TODAY + timedelta(days=5), TODAY) == "09 SEP"
    assert "DUE" not in brief_due_label(TODAY + timedelta(days=5), TODAY)


def test_today_priority_tasks_shows_weekday_for_tomorrow(settings: Settings) -> None:
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[Task(id="1", title="Ship it", due=TODAY + timedelta(days=1), priority=Priority.HIGH)],
        ),
    )
    rows = today_priority_tasks(state, TODAY, settings.max_priority_tasks)
    assert rows[0]["due_label"] == "SAT"
    assert "TOMORROW" not in rows[0]["due_label"]


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
    assert rows[1]["when"] == "SAT 18:00"
    assert "TOMORROW" not in rows[1]["when"]
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
    # The numeral is bare (no percent sign baked in: the template draws that
    # separately, raised); the caption beneath names the window.
    assert claude["value"] == "8"
    assert claude["accent"] == "red"
    assert claude["available"] is True
    assert claude["label"] == "5H"
    # A failed collection is never displayed as a number; the hatch flag
    # takes its place instead, but the caption (and any reset time) stays.
    assert codex["value"] is None
    assert codex["available"] is False


def test_ai_capacity_rows_5h_label_carries_the_reset_time() -> None:
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
                    short_window_reset_at=datetime(2026, 9, 4, 21, 30, tzinfo=tz),
                    weekly_percent_remaining=48,
                    collection_status="ok",
                )
            ],
        ),
    )
    windows = ai_capacity_rows(state)[0]["windows"]
    assert windows[0]["label"] == "5H RESET 21:30"
    # The 7D window never carries a reset time, only its own bare label.
    assert windows[1]["label"] == "7D"


def test_ai_capacity_rows_5h_label_is_bare_without_a_reset_time() -> None:
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
                    short_window_reset_at=None,
                    collection_status="ok",
                )
            ],
        ),
    )
    windows = ai_capacity_rows(state)[0]["windows"]
    assert windows[0]["label"] == "5H"


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


def test_today_next_rows_orders_future_events_and_labels_by_day() -> None:
    """Today's own remaining events first with a bare time, then later days
    with their 3-letter weekday; the word TOMORROW never appears."""
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="past", title="Standup", start=datetime(2026, 9, 4, 9, 30, tzinfo=tz)),
            Event(id="later-today", title="Demo", start=datetime(2026, 9, 4, 16, 30, tzinfo=tz)),
            Event(id="tomorrow", title="Drill", start=datetime(2026, 9, 5, 18, 0, tzinfo=tz)),
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {})
    assert [row["title"] for row in rows] == ["Demo", "Drill"]
    assert rows[0]["when"] == "16:30"
    assert rows[1]["when"] == "SAT 18:00"
    assert "TOMORROW" not in rows[1]["when"]


def test_today_next_rows_all_day_variants() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(
                id="today-allday",
                title="Offsite",
                start=datetime(2026, 9, 4, 0, 0, tzinfo=tz),
                end=datetime(2026, 9, 5, 0, 0, tzinfo=tz),
                all_day=True,
            ),
            Event(
                id="later-allday",
                title="Conference",
                start=datetime(2026, 9, 6, 0, 0, tzinfo=tz),
                all_day=True,
            ),
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {})
    assert rows[0]["when"] == "ALL DAY"
    assert rows[1]["when"] == "SUN"


def test_today_next_rows_caps_at_the_row_limit() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 6, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id=str(n), title=f"Event {n}", start=datetime(2026, 9, 4, 7 + n, 0, tzinfo=tz))
            for n in range(8)
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {}, limit=3)
    assert len(rows) == 3
    assert [row["title"] for row in rows] == ["Event 0", "Event 1", "Event 2"]


def test_today_next_rows_empty_calendar_gives_no_rows() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok")
    assert today_next_rows(state, reference, {}) == []


def test_today_next_rows_shows_placeholder_when_nothing_left_today() -> None:
    """When no remaining event falls today, the first row is a TODAY /
    Nothing left today placeholder, then future days follow."""
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 20, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="past", title="Standup", start=datetime(2026, 9, 4, 9, 30, tzinfo=tz)),
            Event(id="tomorrow", title="Drill", start=datetime(2026, 9, 5, 18, 0, tzinfo=tz)),
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {})
    assert rows[0] == {"when": "TODAY", "title": "Nothing left today", "color": "black"}
    assert rows[1]["title"] == "Drill"
    assert rows[1]["when"] == "SAT 18:00"


def test_today_next_rows_no_placeholder_when_today_has_events() -> None:
    """When today still has a remaining event, no placeholder row appears."""
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[Event(id="later-today", title="Demo", start=datetime(2026, 9, 4, 16, 30, tzinfo=tz))],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {})
    assert all(row["title"] != "Nothing left today" for row in rows)
    assert rows[0]["title"] == "Demo"


def test_today_next_rows_placeholder_counts_against_the_row_limit() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 20, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id=str(n), title=f"Event {n}", start=datetime(2026, 9, 5, 7 + n, 0, tzinfo=tz))
            for n in range(5)
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {}, limit=3)
    assert len(rows) == 3
    assert rows[0]["title"] == "Nothing left today"
    assert [row["title"] for row in rows[1:]] == ["Event 0", "Event 1"]


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


def test_agenda_and_weather_have_no_title_accents_left_to_carry(settings: Settings) -> None:
    """Agenda and weather refuse the pane title bar too: their own route,
    month grid, readings and plates carry state directly."""
    state = empty_state()
    assert build_context("agenda", state, settings)["title_accents"] == {}
    assert build_context("weather", state, settings)["title_accents"] == {}


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
    # Today shows the overdue task itself (the red 1D LATE chip), so the
    # header's own overdue chip would only repeat it.
    assert header["show_overdue_chip"] is False


def test_header_overdue_chip_hidden_on_today_and_brief_shown_elsewhere() -> None:
    """Today and Brief already show the overdue task in their own red 1D
    LATE chip; the header's own chip is only for the pages that do not."""
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    state = DashboardState(
        generated_at=reference,
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[Task(id="1", title="Late", due=TODAY - timedelta(days=1))],
        ),
    )
    today_date = reference.date()
    assert header_context(state, today_date, reference, "today")["show_overdue_chip"] is False
    assert header_context(state, today_date, reference, "brief")["show_overdue_chip"] is False
    assert PAGES_WITH_OWN_OVERDUE_CHIP == {"today", "brief"}
    for page in ("agenda", "weather", "system", "alert"):
        assert header_context(state, today_date, reference, page)["show_overdue_chip"] is True


def test_header_overdue_chip_hidden_when_nothing_is_overdue() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok")
    assert header_context(state, reference.date(), reference, "agenda")["show_overdue_chip"] is False


def test_sensor_value_formatting() -> None:
    assert sensor_value(None, None) == "unknown"
    assert sensor_value("Closed", None) == "Closed"
    assert sensor_value("64", "%") == "64%"
    assert sensor_value("27.8", "C") == "27.8 C"
    # A trailing duration inside free text is capped, matching the panel's
    # own all-caps numerals and units elsewhere ("41M", not "41m").
    assert sensor_value("Clear 41m", None) == "Clear 41M"


def test_cap_duration_only_touches_a_trailing_duration() -> None:
    assert cap_duration("Clear 41m") == "Clear 41M"
    assert cap_duration("Idle") == "Idle"
    assert cap_duration("Home") == "Home"
    assert cap_duration("Ready in 3h") == "Ready in 3H"


# ---------------------------------------------------------------------------
# agenda: the plain event list, the month grid, the next-seven-days strip
# ---------------------------------------------------------------------------
def test_agenda_list_rows_today_all_day_first_then_time_including_past() -> None:
    """Today's own block: all-day events first, then timed events in start
    order, including ones that have already ended (unlike Today's own
    agenda list, which drops them)."""
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(
                id="a",
                title="Morning standup",
                calendar="work",
                start=datetime(2026, 9, 4, 9, 0, tzinfo=tz),
                end=datetime(2026, 9, 4, 9, 15, tzinfo=tz),
            ),
            Event(
                id="b",
                title="Offsite",
                calendar="personal",
                start=datetime(2026, 9, 4, 0, 0, tzinfo=tz),
                end=datetime(2026, 9, 5, 0, 0, tzinfo=tz),
                all_day=True,
            ),
            Event(
                id="c",
                title="Evening class",
                calendar="personal",
                start=datetime(2026, 9, 4, 19, 0, tzinfo=tz),
            ),
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = agenda_list_rows(state, {"work": "blue", "personal": "green"}, today)
    assert [row["kind"] for row in rows] == ["event", "event", "event"]
    assert rows[0]["when"] == "ALL DAY"
    assert rows[1]["when"] == "09:00"  # already ended by noon, still shown
    assert rows[2]["when"] == "19:00"


def test_agenda_list_rows_inserts_a_divider_once_today_runs_out() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="a", title="Standup", start=datetime(2026, 9, 4, 9, 0, tzinfo=tz)),
            Event(id="b", title="Sprint planning", start=datetime(2026, 9, 5, 9, 30, tzinfo=tz)),
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = agenda_list_rows(state, {}, today)
    assert rows[0]["kind"] == "event"
    # The next day is announced by name, never by its weekday.
    assert rows[1] == {"kind": "divider", "label": "TOMORROW"}
    assert rows[2]["kind"] == "event"
    assert rows[2]["when"] == "09:30"


def test_agenda_list_rows_divider_names_weekday_and_date_for_a_later_day() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)  # a Friday
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[Event(id="a", title="QBR", start=datetime(2026, 9, 7, 13, 0, tzinfo=tz))],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = agenda_list_rows(state, {}, today)
    assert rows[0] == {"kind": "divider", "label": "MON 07 SEP"}
    assert rows[1]["when"] == "13:00"


def test_agenda_list_rows_shows_more_when_the_budget_is_exceeded() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 6, 0, tzinfo=tz)
    total_events = AGENDA_LIST_ROW_LIMIT + 4
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(
                id=f"e{i}",
                title=f"Event {i}",
                start=datetime(2026, 9, 4, 7, 0, tzinfo=tz) + timedelta(minutes=i),
            )
            for i in range(total_events)
        ],
    )
    state = DashboardState(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = agenda_list_rows(state, {}, today)
    assert len(rows) == AGENDA_LIST_ROW_LIMIT
    assert rows[-1]["kind"] == "more"
    # N counts only the events cut, not the row it replaces.
    assert rows[-1]["count"] == total_events - AGENDA_LIST_ROW_LIMIT + 1


def test_agenda_list_rows_never_exceeds_the_row_budget() -> None:
    """Many events across many days, so dividers and events both compete for
    the same fixed row budget: the packed list never runs past it."""
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 6, 0, tzinfo=tz)
    items = [
        Event(
            id=f"e{day_offset}-{hour}",
            title=f"Event {day_offset}-{hour}",
            start=datetime(2026, 9, 4, tzinfo=tz) + timedelta(days=day_offset, hours=8 + hour),
        )
        for day_offset in range(10)
        for hour in range(5)
    ]
    state = DashboardState(
        generated_at=reference,
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(status=AdapterStatus.OK, items=items),
    )
    rows = agenda_list_rows(state, {}, today)
    assert len(rows) <= AGENDA_LIST_ROW_LIMIT
    assert rows[-1]["kind"] == "more"


def test_agenda_list_rows_nothing_scheduled_when_the_window_is_empty() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    unusable = DashboardState(generated_at=reference, timezone="Asia/Bangkok")
    assert agenda_list_rows(unusable, {}, today) == []
    usable_but_empty = DashboardState(
        generated_at=reference,
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(status=AdapterStatus.OK, items=[]),
    )
    assert agenda_list_rows(usable_but_empty, {}, today) == []


def test_agenda_context_calendar_note_is_blank_when_the_calendar_is_just_empty(
    settings: Settings,
) -> None:
    """An empty but usable calendar has nothing wrong with it: the note must
    be blank so the template's default "Nothing scheduled" prints."""
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(status=AdapterStatus.OK, items=[]),
    )
    context = agenda_context(state, settings)
    assert context["agenda_list"] == []
    assert context["calendar_note"] == ""


def test_agenda_context_calendar_note_explains_an_unusable_calendar(
    settings: Settings,
) -> None:
    """When the calendar block itself is not usable (unset, or erroring),
    the empty agenda list must say why instead of the generic "Nothing
    scheduled", which would read as "you have no events" rather than "the
    calendar is not configured"."""
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(status=AdapterStatus.UNAVAILABLE),
    )
    context = agenda_context(state, settings)
    assert context["agenda_list"] == []
    assert context["calendar_note"] == block_note(AdapterStatus.UNAVAILABLE, "calendar")
    assert context["calendar_note"] == "calendar unavailable"


def test_month_grid_starts_monday_and_marks_today() -> None:
    today = date(2026, 9, 5)  # a Saturday; 1 Sep 2026 is a Tuesday
    state = DashboardState(
        generated_at=datetime(2026, 9, 5, 8, 0, tzinfo=zone("Asia/Bangkok")), timezone="Asia/Bangkok"
    )
    grid = month_grid(state, {}, today)
    assert grid["name"] == "SEPTEMBER"
    first_week = grid["weeks"][0]
    assert first_week[0] is None
    assert first_week[1]["number"] == 1
    flat = [cell for week in grid["weeks"] for cell in week if cell]
    today_cells = [cell for cell in flat if cell["is_today"]]
    assert [cell["number"] for cell in today_cells] == [5]


def test_month_grid_dots_use_calendar_color_and_black_for_multiple() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 5)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="a", title="Solo", calendar="work", start=datetime(2026, 9, 10, 9, 0, tzinfo=tz)),
            Event(id="b", title="One", calendar="work", start=datetime(2026, 9, 12, 9, 0, tzinfo=tz)),
            Event(id="c", title="Two", calendar="personal", start=datetime(2026, 9, 12, 14, 0, tzinfo=tz)),
        ],
    )
    state = DashboardState(
        generated_at=datetime(2026, 9, 5, 8, 0, tzinfo=tz), timezone="Asia/Bangkok", calendar=events
    )
    grid = month_grid(state, {"work": "blue", "personal": "green"}, today)
    flat = {cell["number"]: cell for week in grid["weeks"] for cell in week if cell}
    assert flat[10]["dot"] == "blue"
    assert flat[12]["dot"] == "black"
    assert flat[11]["dot"] is None


def test_next_seven_days_clamp_busy_bars_and_blank_count() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 5)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            # Starts before 06:00 and runs past 22:00: clamps to a full bar.
            Event(
                id="a",
                title="Overnight",
                start=datetime(2026, 9, 6, 4, 0, tzinfo=tz),
                end=datetime(2026, 9, 6, 23, 0, tzinfo=tz),
            ),
        ],
    )
    state = DashboardState(
        generated_at=datetime(2026, 9, 5, 8, 0, tzinfo=tz), timezone="Asia/Bangkok", calendar=events
    )
    rows = next_seven_days(state, today)
    assert len(rows) == 7
    tomorrow = rows[0]
    assert tomorrow["segments"] == [{"left": 0.0, "width": 100.0}]
    assert tomorrow["count"] == 1
    # A day with nothing on it prints no count at all, not a zero.
    empty_day = rows[1]
    assert empty_day["segments"] == []
    assert empty_day["count"] is None


# ---------------------------------------------------------------------------
# weather: the hero, the four readings, the hourly plates, the daily rows
# ---------------------------------------------------------------------------
def test_weather_summary_hero_states() -> None:
    heat = weather_summary(Weather(condition="Sunny", temperature_c=37.0, rain_from="15:00"))
    assert heat["dot"] == "red"
    rain = weather_summary(Weather(condition="Showers", temperature_c=29.0, rain_probability_percent=60))
    assert rain["dot"] == "blue"
    plain = weather_summary(Weather(condition="Cloudy", temperature_c=28.0, rain_probability_percent=10))
    assert plain["dot"] == ""
    assert weather_summary(None) == {"available": False}


def test_weather_readings_carry_unavailable_flags_and_selective_dots() -> None:
    empty = weather_readings(None)
    assert all(reading["value"] is None for reading in empty)

    full = weather_readings(
        Weather(humidity_percent=76, uv_index=8.4, aqi=71, rain_probability_percent=80)
    )
    by_label = {reading["label"]: reading for reading in full}
    assert by_label["HUMIDITY"]["value"] == "76"
    assert by_label["HUMIDITY"]["dot"] == ""
    assert by_label["UV"]["dot"] == "red"
    assert by_label["AQI"]["dot"] == "yellow"
    assert by_label["RAIN"]["value"] == "80%"
    assert by_label["RAIN"]["dot"] == "blue"

    # A healthy UV reading stays quiet; a healthy AQI still dots (green).
    calm = weather_readings(Weather(uv_index=2.0, aqi=10))
    calm_by_label = {reading["label"]: reading for reading in calm}
    assert calm_by_label["UV"]["dot"] == ""
    assert calm_by_label["AQI"]["dot"] == "green"


def test_weather_hourly_plates_start_at_current_hour_and_cap_at_six() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 14, 30, tzinfo=tz)
    hourly = [
        HourlyRain(at=datetime(2026, 9, 4, hour, 0, tzinfo=tz), probability_percent=hour)
        for hour in range(9, 22)
    ]
    weather = Weather(hourly_rain=hourly)
    plates = weather_hourly_plates(weather, reference)
    assert len(plates) == 6
    assert plates[0]["hour"] == "14"
    assert plates[-1]["hour"] == "19"
    assert weather_hourly_plates(None, reference) == []


def test_weather_daily_rows_cap_at_seven() -> None:
    today = date(2026, 9, 4)
    daily = [
        DailyForecast(day=today + timedelta(days=offset), high_c=30.0, low_c=24.0, rain_probability_percent=10)
        for offset in range(10)
    ]
    weather = Weather(daily=daily)
    rows = weather_daily_rows(weather, today)
    assert len(rows) == 7
    assert rows[0]["label"] == "TODAY"
    assert weather_daily_rows(None, today) == []


# ---------------------------------------------------------------------------
# brief page
# ---------------------------------------------------------------------------
def _brief_state(brief: Brief | None, *, status: AdapterStatus = AdapterStatus.OK) -> DashboardState:
    tz = zone("Asia/Bangkok")
    return DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        brief=BriefBlock(status=status, brief=brief),
    )


def test_brief_mode_label_and_generated_time(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    brief = Brief(
        mode=BriefMode.MORNING,
        generated_at=datetime(2026, 9, 4, 7, 40, tzinfo=tz),
        headline="Two hard deadlines today",
    )
    context = brief_context(_brief_state(brief), settings)
    assert context["mode_label"] == "MORNING BRIEF"
    assert context["generated_label"] == "GENERATED 07:40"


def test_brief_generated_label_is_not_generated_when_missing(settings: Settings) -> None:
    brief = Brief(mode=BriefMode.EVENING, generated_at=None, headline="x")
    context = brief_context(_brief_state(brief), settings)
    assert context["generated_label"] == "NOT GENERATED"


def test_brief_risk_sections_get_flagged(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    brief = Brief(
        mode=BriefMode.MORNING,
        generated_at=datetime(2026, 9, 4, 7, 0, tzinfo=tz),
        headline="x",
        sections=[
            BriefSection(title="At risk", items=["Vendor quote overdue"]),
            BriefSection(title="Key tasks", items=["Finish deck"]),
        ],
    )
    context = brief_context(_brief_state(brief), settings)
    headers = [line for line in context["lines"] if line["kind"] == "header"]
    assert headers[0]["risk"] is True
    assert headers[1]["risk"] is False
    assert brief_is_risk("At risk") is True
    assert brief_is_risk("Key tasks") is False


def test_brief_clipping_produces_an_ellipsis_line() -> None:
    sections = [{"title": "SECTION", "risk": False, "items": [f"Item {n}" for n in range(20)]}]
    lines, truncated = brief_lines(sections, max_lines=5)
    assert truncated is True
    assert len(lines) == 5
    assert lines[-1]["kind"] == "item"
    assert lines[-1]["text"].endswith("...")


def test_brief_lines_never_cuts_leaving_a_bare_header() -> None:
    sections = [
        {"title": "FIRST", "risk": False, "items": ["one", "two"]},
        {"title": "SECOND", "risk": False, "items": ["three"]},
    ]
    # Exactly enough room for the first section's header and items, nothing
    # from the second: the cut must drop the bare "SECOND" header too.
    lines, truncated = brief_lines(sections, max_lines=3)
    assert truncated is True
    assert [line["kind"] for line in lines] == ["header", "item", "item"]


def test_brief_unavailable_message_when_brief_is_missing(settings: Settings) -> None:
    context = brief_context(_brief_state(None, status=AdapterStatus.UNAVAILABLE), settings)
    assert context["brief_available"] is False
    assert context["unavailable_message"] == "No brief from the PC yet"


def test_brief_headline_fits_one_line_thresholds() -> None:
    assert brief_headline_fits_one_line("Rent due Friday") is True
    assert brief_headline_fits_one_line("Two hard deadlines today, storms from 15:00") is False


def test_brief_context_headline_drops_to_24px_when_it_does_not_fit_one_line(
    settings: Settings,
) -> None:
    tz = zone("Asia/Bangkok")
    long_headline = "Two hard deadlines today, storms from 15:00"
    brief = Brief(mode=BriefMode.MORNING, generated_at=datetime(2026, 9, 4, 7, 0, tzinfo=tz), headline=long_headline)
    context = brief_context(_brief_state(brief), settings)
    assert context["headline_large"] is False

    short_headline = "Rent due Friday"
    brief = Brief(mode=BriefMode.MORNING, generated_at=datetime(2026, 9, 4, 7, 0, tzinfo=tz), headline=short_headline)
    context = brief_context(_brief_state(brief), settings)
    assert context["headline_large"] is True


def _tasks_state(titles: list[str]) -> DashboardState:
    tz = zone("Asia/Bangkok")
    return DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[
                Task(id=str(n), title=title, due=None, priority=Priority.LOW)
                for n, title in enumerate(titles)
            ],
        ),
    )


def test_clip_words_returns_text_that_already_fits_unchanged() -> None:
    assert clip_words("Short title", 20) == "Short title"


def test_clip_words_cuts_at_a_word_boundary() -> None:
    """A title too long for its budget is cut at the last whole word, with
    a single ellipsis character, never mid-word."""
    result = clip_words("Send vendor quote answer to K. Somchai", 23)
    assert result == "Send vendor quote…"
    assert not result[:-1].endswith(" ")


def test_clip_words_cuts_a_single_word_longer_than_the_budget() -> None:
    """No word boundary to honour, so the one word itself is cut, exactly
    where ``text-overflow: ellipsis`` would have cut it."""
    result = clip_words("Supercalifragilisticexpialidocious", 10)
    assert result == "Supercali…"
    assert len(result) == 10


def test_clip_words_thai_text_with_spaces() -> None:
    """Thai carries no spaces inside a phrase, so a long phrase is one
    unbroken token: :func:`clip_words` clips it at the budget, the same
    single-word fallback a long English word takes, rather than looking
    forever for a space that is never there."""
    phrase = "ประชุมทีมการตลาดและการขายประจำเดือนกันยายน"
    result = clip_words(phrase, 10)
    assert result == f"{phrase[:9]}…"
    # A short Thai phrase with real spaces between clauses still wraps at
    # a word (clause) boundary like any other text.
    spaced = "ประชุมทีม การตลาด และการขาย"
    result_spaced = clip_words(spaced, 18)
    assert result_spaced == "ประชุมทีม การตลาด…"


def test_brief_task_rows_single_line_for_short_titles() -> None:
    tasks = [{"title": "Ship it"}, {"title": "Call mom"}, {"title": "Water plants"}]
    rows, more = brief_task_rows(tasks)
    assert more == 0
    assert len(rows) == 3
    assert all(row["two_line"] is False for row in rows)


def test_brief_task_rows_wraps_only_the_titles_that_need_it() -> None:
    """Each row decides for itself: a short title stays single-line even
    beside a long one that needs two, unlike the old all-or-nothing switch."""
    tasks = [
        {"title": "Ship it", "chip_kind": "day"},
        {"title": "Submit August expense claim (BTS and Grab)", "chip_kind": "overdue"},
    ]
    rows, more = brief_task_rows(tasks)
    assert more == 0
    assert rows[0]["two_line"] is False
    assert rows[0]["title"] == "Ship it"
    assert rows[1]["two_line"] is True
    assert rows[1]["title"] != tasks[1]["title"]


def test_brief_task_rows_reports_the_hidden_count_when_the_column_is_full() -> None:
    """Seven real, long task titles (the fixture's own) do not all fit two
    lines each in the column: the ones that do not fit are folded into a
    "+N MORE" count instead of overflowing or shrinking rows further."""
    long_titles = [
        "Finish Q3 OKR review deck for Monday",
        "Send vendor quote answer to K. Somchai",
        "Review PR 482 auth refactor",
        "Submit August expense claim (BTS and Grab)",
        "Renew work permit documents at HR",
        "Back up NAS photo library to cold storage",
        "Book dentist appointment near Asok",
    ]
    tasks = [{"title": title} for title in long_titles]
    rows, more = brief_task_rows(tasks)
    assert len(rows) + more == len(tasks)
    assert more > 0
    for row, title in zip(rows, long_titles):
        if row["title"] != title:
            # Never a mid-word stem: the kept text plus ellipsis is a clean
            # prefix of the real title.
            body = row["title"][:-1]
            assert title.startswith(body)


def test_due_chip_kind_matches_due_labels_own_branching() -> None:
    today = date(2026, 9, 4)
    assert due_chip_kind(None, today) == "none"
    assert due_chip_kind(today - timedelta(days=1), today) == "overdue"
    assert due_chip_kind(today, today) == "today"
    assert due_chip_kind(today + timedelta(days=1), today) == "day"
    assert due_chip_kind(today + timedelta(days=5), today) == "date"


def test_priority_title_budget_ordered_by_chip_width() -> None:
    """A wider chip always leaves a title the same size or narrower budget
    than a lighter one; the widest chip's budget never dips below the old
    uniform floor."""
    kinds = sorted(TODAY_CHIP_WIDTH_PX, key=lambda kind: TODAY_CHIP_WIDTH_PX[kind], reverse=True)
    budgets = [priority_title_budget(kind) for kind in kinds]
    assert budgets == sorted(budgets)
    assert min(budgets) >= PRIORITY_TITLE_MAX_CHARS


def test_brief_title_budget_ordered_by_chip_width() -> None:
    kinds = sorted(BRIEF_CHIP_WIDTH_PX, key=lambda kind: BRIEF_CHIP_WIDTH_PX[kind], reverse=True)
    budgets = [brief_title_budget(kind) for kind in kinds]
    assert budgets == sorted(budgets)
    assert min(budgets) >= BRIEF_TITLE_MAX_CHARS


def test_today_priorities_title_beside_a_weekday_chip_is_not_clipped(settings: Settings) -> None:
    """The fixture's own regression: a title that stemmed beside a narrow
    "MON" chip under the old uniform budget now reads whole, because that
    row's own (lighter) chip hands its title the extra room."""
    today = date(2026, 9, 4)
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[
                Task(
                    id="1",
                    title="Review PR 482 auth refactor",
                    due=today + timedelta(days=1),
                    priority=Priority.HIGH,
                )
            ],
        ),
    )
    rows = today_priority_tasks(state, today, 5)
    assert rows[0]["title"] == "Review PR 482 auth refactor"


# ---------------------------------------------------------------------------
# system page
# ---------------------------------------------------------------------------
def test_battery_accent_thresholds() -> None:
    assert battery_accent(None) == "black"
    assert battery_accent(21) == "black"
    assert battery_accent(20) == "yellow"
    assert battery_accent(11) == "yellow"
    assert battery_accent(10) == "red"


def _device_state(**overrides: Any) -> DeviceState:
    base: dict[str, Any] = dict(
        status=DeviceStatus.OK,
        device="reterminal-e1002",
        received_at=datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc),
        age_seconds=30.0,
    )
    base.update(overrides)
    return DeviceState(**base)


def test_device_panel_hatches_the_battery_block_when_level_is_none(settings: Settings) -> None:
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc),
        timezone="Asia/Bangkok",
        device=DeviceBlock(status=AdapterStatus.OK, device=_device_state(battery_level=None)),
    )
    panel = device_panel(state, settings)
    assert panel["available"] is True
    assert panel["battery_available"] is False


def test_device_panel_flags_stale_with_a_tell_tale_and_age(settings: Settings) -> None:
    now = datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc)
    device = _device_state(
        status=DeviceStatus.STALE, received_at=now - timedelta(hours=2), age_seconds=7200.0
    )
    state = DashboardState(
        generated_at=now, timezone="Asia/Bangkok", device=DeviceBlock(status=AdapterStatus.OK, device=device)
    )
    panel = device_panel(state, settings)
    assert panel["stale"] is True
    assert panel["age_text"] == "2 H AGO"
    assert desk_accent(state) == "yellow"


def test_wifi_level_glyph_choice_matches_the_system_reading() -> None:
    assert wifi_level(-60) == "strong"
    assert wifi_level(-75) == "low"
    assert wifi_level(None) == "off"
    assert wifi_accent(-60) == "green"
    assert wifi_accent(-75) == "yellow"
    assert wifi_accent(None) == "black"


def test_sensor_accent_only_flags_warn_and_alert() -> None:
    assert sensor_accent("ok") == ""
    assert sensor_accent("unknown") == ""
    assert sensor_accent("warn") == "yellow"
    assert sensor_accent("alert") == "red"


def test_service_mark_per_health() -> None:
    assert service_mark("ok") == "black"
    assert service_mark("warn") == "yellow"
    assert service_mark("down") == "red"
    assert service_mark("unknown") == "hatch"


def test_system_context_sensor_and_service_rows(settings: Settings) -> None:
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc),
        timezone="Asia/Bangkok",
        home=HomeBlock(
            status=AdapterStatus.OK,
            home=HomeState(
                sensors=[
                    HomeSensor(key="front_door", name="Front door", value="Closed", severity="ok")
                ],
                services=[ServiceStatus(key="nas", name="NAS", health=ServiceHealth.DOWN)],
            ),
        ),
    )
    context = system_context(state, settings)
    home_rows = context["home_rows"]
    sensor_row = next(row for row in home_rows if row["kind"] == "sensor")
    service_row = next(row for row in home_rows if row["kind"] == "service")
    assert sensor_row["accent"] == ""
    assert service_row["mark"] == "red"
    assert service_row["down"] is True
    # Sensors first: with one of each, the sensor row leads the merged list.
    assert home_rows[0]["kind"] == "sensor"
    assert home_rows[1]["kind"] == "service"


def test_home_rows_merge_sensors_and_services_under_one_budget(settings: Settings) -> None:
    """A merged HOME column with more sensors and services than fit is
    capped to HOME_ROW_BUDGET total, sensors first, not per half."""
    sensors = [
        HomeSensor(key=f"s{i}", name=f"Sensor {i}", value="1", severity="ok")
        for i in range(HOME_ROW_BUDGET)
    ]
    services = [
        ServiceStatus(key=f"svc{i}", name=f"Service {i}", health=ServiceHealth.OK)
        for i in range(HOME_ROW_BUDGET)
    ]
    state = DashboardState(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc),
        timezone="Asia/Bangkok",
        home=HomeBlock(status=AdapterStatus.OK, home=HomeState(sensors=sensors, services=services)),
    )
    context = system_context(state, settings)
    home_rows = context["home_rows"]
    assert len(home_rows) == HOME_ROW_BUDGET
    assert all(row["kind"] == "sensor" for row in home_rows)


def test_dataset_age_label_buckets() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    assert dataset_age_label(None, now) == "NEVER"
    assert dataset_age_label(now, now) == "NOW"
    assert dataset_age_label(now - timedelta(minutes=5), now) == "5 MIN"
    assert dataset_age_label(now - timedelta(hours=2), now) == "2 H"
    assert dataset_age_label(now - timedelta(days=3), now) == "3 D"


def test_strip_scheme_drops_the_scheme_only() -> None:
    assert strip_scheme("https://192.0.2.10:8080") == "192.0.2.10:8080"
    assert strip_scheme("http://hub.local") == "hub.local"


def test_hub_rows_mark_a_stale_pushed_dataset_yellow(settings: Settings) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    state = DashboardState(
        generated_at=now,
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            source="agent",
            updated_at=now - timedelta(hours=20),
            received_at=now - timedelta(hours=20),
        ),
    )
    rows = hub_rows(state, settings, now)
    tasks_row = next(row for row in rows if row["name"] == "TASKS")
    assert tasks_row["value"] == "20 H"
    assert tasks_row["accent"] == "yellow"


def test_hub_rows_device_origin_from_device_state(settings: Settings) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    device = DeviceState(
        status=DeviceStatus.OK,
        device="reterminal-e1002",
        received_at=now,
        age_seconds=0.0,
        newest_at=now - timedelta(minutes=5),
        remote_addr="192.0.2.10",
        hub_host="https://192.0.2.1:8080",
    )
    state = DashboardState(
        generated_at=now,
        timezone="Asia/Bangkok",
        device=DeviceBlock(status=AdapterStatus.OK, device=device),
    )
    rows = hub_rows(state, settings, now)
    by_name = {row["name"]: row for row in rows}
    assert by_name["DEVICE SYNC"]["value"] == "5 MIN"
    assert by_name["DEVICE IP"]["value"] == "192.0.2.10"
    assert by_name["HUB URL"]["value"] == "192.0.2.1:8080"


def test_power_label_words_feed_the_battery_meter_caption() -> None:
    assert power_label(False, None) == "BATTERY"
    assert power_label(True, "charging") == "CHARGING"
    assert power_label(True, "charged") == "USB"
    assert power_label(True, "unknown") is None


def test_wake_label_spaces_out_the_firmware_word() -> None:
    assert wake_label(None) == "UNKNOWN"
    assert wake_label("button_left") == "BUTTON LEFT"


# ---------------------------------------------------------------------------
# alert page
# ---------------------------------------------------------------------------
def _alert_state(alert: Alert | None) -> DashboardState:
    return DashboardState(
        generated_at=datetime(2026, 9, 4, 15, 6, tzinfo=dt_timezone.utc),
        timezone="Asia/Bangkok",
        alert=alert,
    )


def test_alert_band_colour_per_priority(settings: Settings) -> None:
    assert ALERT_BAND_ACCENT[AlertPriority.CRITICAL] == "red"
    assert ALERT_BAND_ACCENT[AlertPriority.DOORBELL] == "red"
    assert ALERT_BAND_ACCENT[AlertPriority.IMPORTANT] == "yellow"
    assert ALERT_BAND_ACCENT[AlertPriority.NORMAL] == "black"

    tz = zone("Asia/Bangkok")
    alert = Alert(
        title="Smoke", priority=AlertPriority.CRITICAL, created_at=datetime(2026, 9, 4, 15, 6, tzinfo=tz)
    )
    context = alert_context(_alert_state(alert), settings)
    assert context["band_accent"] == "red"


def test_alert_band_label_falls_back_to_alert_without_a_source(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    unnamed = Alert(
        title="Doorbell",
        priority=AlertPriority.DOORBELL,
        created_at=datetime(2026, 9, 4, 15, 6, tzinfo=tz),
        source=None,
    )
    assert alert_context(_alert_state(unnamed), settings)["band_label"] == "ALERT"

    named = Alert(
        title="Doorbell",
        priority=AlertPriority.DOORBELL,
        created_at=datetime(2026, 9, 4, 15, 6, tzinfo=tz),
        source="Front door",
    )
    assert alert_context(_alert_state(named), settings)["band_label"] == "FRONT DOOR"


def test_alert_band_right_carries_the_priority_word_and_time(settings: Settings) -> None:
    tz = zone("Asia/Bangkok")
    alert = Alert(
        title="Smoke", priority=AlertPriority.CRITICAL, created_at=datetime(2026, 9, 4, 15, 6, tzinfo=tz)
    )
    context = alert_context(_alert_state(alert), settings)
    assert context["band_right"] == "CRITICAL 15:06"


def test_alert_context_without_an_active_alert(settings: Settings) -> None:
    context = alert_context(_alert_state(None), settings)
    assert context["band_label"] == "ALERT"
    assert context["band_accent"] == "black"
    assert context["title"] == "NO ACTIVE ALERT"


# ---------------------------------------------------------------------------
# Staleness (Part 2)
# ---------------------------------------------------------------------------
NOW = datetime(2026, 9, 4, 20, 0, tzinfo=zone("Asia/Bangkok"))


def test_stale_info_is_none_when_fresh() -> None:
    reference = NOW - timedelta(hours=2)
    assert stale_info(reference, "file", threshold_seconds=21600, now=NOW) is None


def test_stale_info_is_none_for_a_fixture_regardless_of_age() -> None:
    reference = NOW - timedelta(days=30)
    assert stale_info(reference, "fixture", threshold_seconds=1, now=NOW) is None


def test_stale_info_is_none_when_the_reference_is_unknown() -> None:
    assert stale_info(None, "file", threshold_seconds=1, now=NOW) is None


def test_stale_info_is_none_under_one_hour_even_past_the_threshold() -> None:
    reference = NOW - timedelta(minutes=30)
    assert stale_info(reference, "file", threshold_seconds=60, now=NOW) is None


def test_stale_info_buckets_to_whole_hours() -> None:
    reference = NOW - timedelta(hours=6, minutes=45)
    assert stale_info(reference, "file", threshold_seconds=21600, now=NOW) == "6 H AGO"


def _empty_state_with(**blocks: Any) -> DashboardState:
    return DashboardState(generated_at=NOW, timezone="Asia/Bangkok", **blocks)


def test_ai_usage_stale_uses_the_oldest_providers_collected_at(settings: Settings) -> None:
    old = NOW - timedelta(hours=7)
    newer = NOW - timedelta(hours=1)
    state = _empty_state_with(
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            source="file",
            providers=[
                AIUsage(provider="claude", collected_at=newer),
                AIUsage(provider="codex", collected_at=old),
            ],
        )
    )
    assert ai_usage_stale(state, settings, NOW) == "7 H AGO"


def test_ai_usage_stale_is_none_for_a_fixture(settings: Settings) -> None:
    old = NOW - timedelta(days=10)
    state = _empty_state_with(
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            source="fixture",
            providers=[AIUsage(provider="claude", collected_at=old)],
        )
    )
    assert ai_usage_stale(state, settings, NOW) is None


def test_brief_stale_uses_generated_at(settings: Settings) -> None:
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(
        brief=BriefBlock(
            status=AdapterStatus.OK,
            source="file",
            brief=Brief(headline="x", generated_at=old, source="file"),
        )
    )
    assert brief_stale(state, settings, NOW) == "11 H AGO"


def test_brief_stale_is_none_without_a_brief(settings: Settings) -> None:
    state = _empty_state_with(brief=BriefBlock(status=AdapterStatus.UNAVAILABLE, source="file"))
    assert brief_stale(state, settings, NOW) is None


def test_tasks_stale_uses_received_at(settings: Settings) -> None:
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(tasks=TasksBlock(status=AdapterStatus.OK, source="file", received_at=old))
    assert tasks_stale(state, settings, NOW) == "11 H AGO"


def test_tasks_stale_is_none_for_a_fixture(settings: Settings) -> None:
    old = NOW - timedelta(days=5)
    state = _empty_state_with(tasks=TasksBlock(status=AdapterStatus.OK, source="fixture", received_at=old))
    assert tasks_stale(state, settings, NOW) is None


def test_window_flags_adds_today_when_ai_usage_is_stale(settings: Settings) -> None:
    old = NOW - timedelta(hours=7)
    state = _empty_state_with(
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            source="file",
            providers=[AIUsage(provider="claude", collected_at=old)],
        )
    )
    assert "today" in window_flags(state, settings, NOW, overdue_count=0)


def test_window_flags_adds_brief_when_tasks_is_stale(settings: Settings) -> None:
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(tasks=TasksBlock(status=AdapterStatus.OK, source="file", received_at=old))
    assert "brief" in window_flags(state, settings, NOW, overdue_count=0)


def test_window_flags_adds_today_when_brief_is_stale(settings: Settings) -> None:
    """Today draws the brief note too (view.py:brief_note), so a stale brief
    must flag Today's own footer entry, not only Brief's."""
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(
        brief=BriefBlock(
            status=AdapterStatus.OK,
            source="file",
            brief=Brief(headline="x", generated_at=old, source="file"),
        )
    )
    assert "today" in window_flags(state, settings, NOW, overdue_count=0)


def test_window_flags_does_not_flag_today_or_brief_when_nothing_is_stale(settings: Settings) -> None:
    flagged = window_flags(_empty_state_with(), settings, NOW, overdue_count=0)
    assert "today" not in flagged
    assert "brief" not in flagged


def test_page_shows_demo_data_checks_only_that_pages_own_datasets(settings: Settings) -> None:
    state = _empty_state_with(
        ai_usage=AIUsageBlock(status=AdapterStatus.OK, source="fixture"),
        brief=BriefBlock(
            status=AdapterStatus.OK, source="file", brief=Brief(headline="x", source="file")
        ),
        tasks=TasksBlock(status=AdapterStatus.OK, source="file"),
    )
    assert page_shows_demo_data(state, "today") is True  # ai_usage is fixture
    assert page_shows_demo_data(state, "brief") is False  # brief and tasks are both file
    assert page_shows_demo_data(state, "agenda") is False  # agenda has no tracked dataset


def test_footer_context_demo_flag(settings: Settings) -> None:
    state = _empty_state_with(ai_usage=AIUsageBlock(status=AdapterStatus.OK, source="fixture"))
    footer = footer_context(state, settings, NOW.date(), NOW, "today")
    assert footer["demo"] is True
    footer = footer_context(state, settings, NOW.date(), NOW, "agenda")
    assert footer["demo"] is False


def test_today_context_carries_the_stale_labels(settings: Settings) -> None:
    old = NOW - timedelta(hours=7)
    state = _empty_state_with(
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            source="file",
            providers=[AIUsage(provider="claude", collected_at=old)],
        )
    )
    context = today_context(state, settings)
    assert context["capacity_stale"] == "7 H AGO"
    assert context["priorities_stale"] is None


def test_brief_context_carries_the_stale_label(settings: Settings) -> None:
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(
        brief=BriefBlock(
            status=AdapterStatus.OK,
            source="file",
            brief=Brief(headline="x", generated_at=old, source="file"),
        )
    )
    context = brief_context(state, settings)
    assert context["brief_stale"] == "11 H AGO"
