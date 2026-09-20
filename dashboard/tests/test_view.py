"""View helpers shared by more than one page, or by core itself: unknown
handling, task ordering shared between Today and Brief, the header/footer,
the alert page, and the stale rules.

Page-specific tests moved into tests/modules/test_<id>.py in 2.2
(docs/plan/2026-09-19-settings-modules-provisioning.md), alongside the
context builders they exercise.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any


from app import icons
from app.settings import HubSettings
from app.models import (
    AdapterStatus,
    AIUsage,
    AIUsageBlock,
    Alert,
    AlertPriority,
    Brief,
    BriefBlock,
    DashboardState,
    DeviceBlock,
    DeviceState,
    DeviceStatus,
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
from app.modules.agenda.page import agenda_flag
from app.modules.brief.page import brief_flag
from app.modules.system.page import system_flag
from app.modules.today.page import today_flag
from app.modules.weather.page import weather_flag
from app.timeutil import zone
from app.view import (
    ALERT_BAND_ACCENT,
    PAGES_WITH_OWN_OVERDUE_CHIP,
    ai_usage_stale,
    alert_context,
    brief_stale,
    build_context,
    clip_words,
    due_chip_kind,
    due_label,
    footer_context,
    header_context,
    header_weather,
    meter_cells,
    page_shows_demo_data,
    stale_info,
    task_accent,
    task_sort_key,
    tasks_stale,
    wifi_level,
    worst_accent,
)
from tests.conftest import make_state


TODAY = date(2026, 9, 4)


def empty_state() -> DashboardState:
    """A state where every adapter failed. Pages must still render."""
    tz = zone("Asia/Bangkok")
    return make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
    )


def test_every_block_defaults_to_unavailable() -> None:
    state = empty_state()
    assert all(block.status is AdapterStatus.UNAVAILABLE for block in state.blocks.values())
    assert not any(block.usable for block in state.blocks.values())


def test_pages_render_context_when_everything_is_unavailable(hub_settings: HubSettings) -> None:
    state = empty_state()
    for page in ("today", "agenda", "weather", "brief", "system", "alert"):
        context = build_context(page, state, hub_settings)
        assert context["page"] == page
    today = build_context("today", state, hub_settings)
    assert today["priorities"] == []
    assert today["header"]["weather"]["available"] is False
    assert "unavailable" in today["ai_note"]["text"]
    weather = build_context("weather", state, hub_settings)
    assert weather["hero"]["available"] is False
    brief = build_context("brief", state, hub_settings)
    assert brief["brief_available"] is False
    assert brief["lines"] == []
    system = build_context("system", state, hub_settings)
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


def test_header_weather_without_data_is_the_hatch_flag() -> None:
    state = empty_state()
    assert header_weather(state) == {"available": False}


def test_header_weather_marks_a_stale_block_as_unavailable() -> None:
    state = empty_state()
    state.blocks["weather"] = WeatherBlock(status=AdapterStatus.STALE, weather=None)
    assert header_weather(state)["available"] is False


def test_header_weather_rain_gives_blue_and_the_rain_label() -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
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
    state = make_state(
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
    state = make_state(
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


def test_every_page_carries_the_header_and_the_window_list(hub_settings: HubSettings) -> None:
    state = empty_state()
    for page in ("today", "agenda", "weather", "brief", "system", "alert"):
        context = build_context(page, state, hub_settings)
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


def test_overdue_segment_counts_only_open_late_tasks(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
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
    context = build_context("weather", state, hub_settings)
    assert context["header"]["overdue_count"] == 2


def _flagged(state: DashboardState, hub_settings: HubSettings) -> set[str]:
    return {
        window["name"]
        for window in build_context("today", state, hub_settings)["footer"]["windows"]
        if window["flag"]
    }


def _flag_state(**blocks: Any) -> DashboardState:
    tz = zone("Asia/Bangkok")
    return make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        **blocks,
    )


def test_window_list_flags_the_pages_that_need_attention(hub_settings: HubSettings) -> None:
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
    assert _flagged(state, hub_settings) == {"AGENDA", "WEATHER", "SYSTEM"}


def test_window_list_flags_are_selective(hub_settings: HubSettings) -> None:
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
    assert _flagged(state, hub_settings) == set()


def test_window_list_flags_system_when_the_paper_goes_quiet(hub_settings: HubSettings) -> None:
    now = datetime(2026, 9, 4, 12, 0, tzinfo=dt_timezone.utc)
    stale = DeviceState(
        status=DeviceStatus.STALE,
        device="reterminal-e1002",
        received_at=now,
        age_seconds=7200.0,
        temperature=30.0,
    )
    state = _flag_state(device=DeviceBlock(status=AdapterStatus.OK, source="store", device=stale))
    assert _flagged(state, hub_settings) == {"SYSTEM"}


def test_window_list_has_no_flags_when_nothing_needs_attention(hub_settings: HubSettings) -> None:
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
    context = build_context("today", state, hub_settings)
    assert not any(window["flag"] for window in context["footer"]["windows"])


def test_worst_accent_takes_the_loudest() -> None:
    assert worst_accent(["green", "red", "yellow"]) == "red"
    assert worst_accent(["green", "yellow"]) == "yellow"
    assert worst_accent(["green", "black"]) == "green"
    assert worst_accent(["black"]) == "black"
    assert worst_accent([]) == "black"


def test_today_has_no_title_accents_left_to_carry(hub_settings: HubSettings) -> None:
    """Today refuses the pane title bar entirely: nothing fills it any more."""
    state = empty_state()
    assert build_context("today", state, hub_settings)["title_accents"] == {}


def test_agenda_and_weather_have_no_title_accents_left_to_carry(hub_settings: HubSettings) -> None:
    """Agenda and weather refuse the pane title bar too: their own route,
    month grid, readings and plates carry state directly."""
    state = empty_state()
    assert build_context("agenda", state, hub_settings)["title_accents"] == {}
    assert build_context("weather", state, hub_settings)["title_accents"] == {}


def test_header_carries_the_overdue_flag_and_a_neutral_battery(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[Task(id="1", title="Late", due=TODAY - timedelta(days=1))],
        ),
    )
    header = build_context("today", state, hub_settings)["header"]
    assert header["day"] == "04"
    assert header["weekday"] == "FRI"
    assert header["month"] == "SEP"
    assert header["overdue_count"] == 1
    # No device at all: the battery reading is neutral, never a guessed chip.
    assert header["battery"]["chip_accent"] == ""
    # Today shows the overdue task itself (the red 1D LATE chip), so the
    # header's own overdue chip would only repeat it.
    assert header["show_overdue_chip"] is False


def test_header_battery_shows_the_plug_icon_on_usb(hub_settings: HubSettings) -> None:
    """usb_present true gives the plug glyph, false gives the battery glyphs,
    and None (older firmware, or no device at all) keeps today's battery
    reading, matching icons.battery_icon's own tri-state rule."""
    tz = zone("Asia/Bangkok")
    now = datetime(2026, 9, 4, 8, 0, tzinfo=tz)

    def _header(usb_present: bool | None) -> dict[str, Any]:
        device = DeviceState(
            status=DeviceStatus.OK,
            device="reterminal-e1002",
            received_at=now,
            age_seconds=30.0,
            battery_level=80.0,
            usb_present=usb_present,
        )
        state = make_state(
            generated_at=now,
            timezone="Asia/Bangkok",
            device=DeviceBlock(status=AdapterStatus.OK, device=device),
        )
        return header_context(state, now.date(), now, "today")

    plugged_in = _header(True)
    assert plugged_in["battery"]["icon"] == icons.POWER_PLUG

    on_battery = _header(False)
    assert on_battery["battery"]["icon"] != icons.POWER_PLUG

    unreported = _header(None)
    assert unreported["battery"]["icon"] == icons.battery_icon(80.0)

    no_device = header_context(empty_state(), now.date(), now, "today")
    assert no_device["battery"]["icon"] == icons.battery_icon(None)


def test_header_overdue_chip_hidden_on_today_and_brief_shown_elsewhere() -> None:
    """Today and Brief already show the overdue task in their own red 1D
    LATE chip; the header's own chip is only for the pages that do not."""
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    state = make_state(
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
    state = make_state(generated_at=reference, timezone="Asia/Bangkok")
    assert header_context(state, reference.date(), reference, "agenda")["show_overdue_chip"] is False


def _tasks_state(titles: list[str]) -> DashboardState:
    tz = zone("Asia/Bangkok")
    return make_state(
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


def test_due_chip_kind_matches_due_labels_own_branching() -> None:
    today = date(2026, 9, 4)
    assert due_chip_kind(None, today) == "none"
    assert due_chip_kind(today - timedelta(days=1), today) == "overdue"
    assert due_chip_kind(today, today) == "today"
    assert due_chip_kind(today + timedelta(days=1), today) == "day"
    assert due_chip_kind(today + timedelta(days=5), today) == "date"


def _alert_state(alert: Alert | None) -> DashboardState:
    return make_state(
        generated_at=datetime(2026, 9, 4, 15, 6, tzinfo=dt_timezone.utc),
        timezone="Asia/Bangkok",
        alert=alert,
    )


def test_alert_band_colour_per_priority(hub_settings: HubSettings) -> None:
    assert ALERT_BAND_ACCENT[AlertPriority.CRITICAL] == "red"
    assert ALERT_BAND_ACCENT[AlertPriority.DOORBELL] == "red"
    assert ALERT_BAND_ACCENT[AlertPriority.IMPORTANT] == "yellow"
    assert ALERT_BAND_ACCENT[AlertPriority.NORMAL] == "black"

    tz = zone("Asia/Bangkok")
    alert = Alert(
        title="Smoke", priority=AlertPriority.CRITICAL, created_at=datetime(2026, 9, 4, 15, 6, tzinfo=tz)
    )
    context = alert_context(_alert_state(alert), hub_settings)
    assert context["band_accent"] == "red"


def test_alert_band_label_falls_back_to_alert_without_a_source(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    unnamed = Alert(
        title="Doorbell",
        priority=AlertPriority.DOORBELL,
        created_at=datetime(2026, 9, 4, 15, 6, tzinfo=tz),
        source=None,
    )
    assert alert_context(_alert_state(unnamed), hub_settings)["band_label"] == "ALERT"

    named = Alert(
        title="Doorbell",
        priority=AlertPriority.DOORBELL,
        created_at=datetime(2026, 9, 4, 15, 6, tzinfo=tz),
        source="Front door",
    )
    assert alert_context(_alert_state(named), hub_settings)["band_label"] == "FRONT DOOR"


def test_alert_band_right_carries_the_priority_word_and_time(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    alert = Alert(
        title="Smoke", priority=AlertPriority.CRITICAL, created_at=datetime(2026, 9, 4, 15, 6, tzinfo=tz)
    )
    context = alert_context(_alert_state(alert), hub_settings)
    assert context["band_right"] == "CRITICAL 15:06"


def test_alert_context_without_an_active_alert(hub_settings: HubSettings) -> None:
    context = alert_context(_alert_state(None), hub_settings)
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
    return make_state(generated_at=NOW, timezone="Asia/Bangkok", **blocks)


def test_ai_usage_stale_uses_the_oldest_providers_collected_at(hub_settings: HubSettings) -> None:
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
    assert ai_usage_stale(state, hub_settings, NOW) == "7 H AGO"


def test_ai_usage_stale_is_none_for_a_fixture(hub_settings: HubSettings) -> None:
    old = NOW - timedelta(days=10)
    state = _empty_state_with(
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            source="fixture",
            providers=[AIUsage(provider="claude", collected_at=old)],
        )
    )
    assert ai_usage_stale(state, hub_settings, NOW) is None


def test_brief_stale_uses_generated_at(hub_settings: HubSettings) -> None:
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(
        brief=BriefBlock(
            status=AdapterStatus.OK,
            source="file",
            brief=Brief(headline="x", generated_at=old, source="file"),
        )
    )
    assert brief_stale(state, hub_settings, NOW) == "11 H AGO"


def test_brief_stale_is_none_without_a_brief(hub_settings: HubSettings) -> None:
    state = _empty_state_with(brief=BriefBlock(status=AdapterStatus.UNAVAILABLE, source="file"))
    assert brief_stale(state, hub_settings, NOW) is None


def test_tasks_stale_uses_received_at(hub_settings: HubSettings) -> None:
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(tasks=TasksBlock(status=AdapterStatus.OK, source="file", received_at=old))
    assert tasks_stale(state, hub_settings, NOW) == "11 H AGO"


def test_tasks_stale_is_none_for_a_fixture(hub_settings: HubSettings) -> None:
    old = NOW - timedelta(days=5)
    state = _empty_state_with(tasks=TasksBlock(status=AdapterStatus.OK, source="fixture", received_at=old))
    assert tasks_stale(state, hub_settings, NOW) is None


def test_no_page_is_flagged_when_nothing_is_stale_or_broken(hub_settings: HubSettings) -> None:
    state = _empty_state_with()
    assert today_flag(state, hub_settings) is False
    assert brief_flag(state, hub_settings) is False
    assert agenda_flag(state, hub_settings) is False
    assert weather_flag(state, hub_settings) is False
    assert system_flag(state, hub_settings) is False


def test_page_shows_demo_data_checks_only_that_pages_own_datasets(hub_settings: HubSettings) -> None:
    state = _empty_state_with(
        ai_usage=AIUsageBlock(status=AdapterStatus.OK, source="fixture"),
        brief=BriefBlock(
            status=AdapterStatus.OK, source="file", brief=Brief(headline="x", source="file")
        ),
        tasks=TasksBlock(status=AdapterStatus.OK, source="file"),
    )
    # Each page's own demo_datasets, as its PageSpec carries them.
    assert page_shows_demo_data(state, ("ai_usage", "brief", "tasks")) is True
    assert page_shows_demo_data(state, ("brief", "tasks")) is False
    assert page_shows_demo_data(state, ()) is False


def test_footer_context_demo_flag(hub_settings: HubSettings) -> None:
    state = _empty_state_with(ai_usage=AIUsageBlock(status=AdapterStatus.OK, source="fixture"))
    footer = footer_context(state, hub_settings, NOW.date(), NOW, "today")
    assert footer["demo"] is True
    footer = footer_context(state, hub_settings, NOW.date(), NOW, "agenda")
    assert footer["demo"] is False
