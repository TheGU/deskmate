"""Fixture adapters, failure handling and the derived weather helpers."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from app.adapters import ai_brief
from app.adapters.ai_brief import FileBriefAdapter, FixtureBriefAdapter, parse_markdown_brief
from app.adapters.ai_usage import FileAIUsageAdapter, FixtureAIUsageAdapter
from app.adapters.base import AdapterUnavailable, CachedAdapter
from app.adapters.calendar import FixtureCalendarAdapter, IcsCalendarAdapter, parse_ics
from app.adapters.home_assistant import FixtureHomeAdapter, RestHomeAdapter, build_home_state
from app.adapters.tasks import FixtureTasksAdapter, ObsidianTasksAdapter
from app.adapters.weather import (
    FixtureWeatherAdapter,
    OpenMeteoWeatherAdapter,
    aqi_label,
    condition_from_code,
    rain_window,
)
from app.config import Settings
from app.models import AdapterStatus, BriefMode, HourlyRain, Priority, ServiceHealth
from app.timeutil import today_local, zone
from tests.conftest import run


# -- tasks -----------------------------------------------------------------
def test_fixture_tasks_are_shifted_to_today(settings: Settings) -> None:
    tasks = run(FixtureTasksAdapter(settings).fetch())
    assert tasks
    today = today_local(settings.timezone)
    dues = {task.due for task in tasks if task.due is not None}
    assert today in dues, "the anchor task should land on today"
    assert any(task.due is not None and task.due < today for task in tasks), "one overdue task"
    assert {task.source for task in tasks} == {"fixture"}
    assert any(task.priority is Priority.HIGH for task in tasks)
    assert any(task.completed for task in tasks)


def test_fixture_tasks_keep_literal_dates_when_shifting_is_off(settings: Settings) -> None:
    literal = settings.model_copy(update={"fixture_relative_dates": False})
    tasks = run(FixtureTasksAdapter(literal).fetch())
    assert any(task.due == date(2026, 9, 4) for task in tasks)


def test_obsidian_adapter_without_a_vault_is_unavailable(settings: Settings) -> None:
    adapter = ObsidianTasksAdapter(settings.model_copy(update={"obsidian_vault_path": None}))
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


# -- calendar --------------------------------------------------------------
def test_fixture_calendar_is_sorted_and_localized(settings: Settings) -> None:
    events = run(FixtureCalendarAdapter(settings).fetch())
    assert events
    assert events == sorted(events, key=lambda event: (event.start, event.title))
    assert all(event.start.tzinfo is not None for event in events)
    assert any(event.all_day for event in events)


def test_ics_adapter_without_urls_is_unavailable(settings: Settings) -> None:
    with pytest.raises(AdapterUnavailable):
        run(IcsCalendarAdapter(settings).fetch())


ICS_SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//deskmate//test//EN
BEGIN:VEVENT
UID:one@example.com
SUMMARY:Standup
DTSTART:20260904T023000Z
DTEND:20260904T024500Z
LOCATION:Meet
END:VEVENT
BEGIN:VEVENT
UID:two@example.com
SUMMARY:All day offsite
DTSTART;VALUE=DATE:20260905
DTEND;VALUE=DATE:20260906
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_reads_timed_and_all_day_events(settings: Settings) -> None:
    tz = zone(settings.timezone)
    window_start = datetime(2026, 9, 1, tzinfo=tz)
    window_end = datetime(2026, 9, 30, tzinfo=tz)
    events = parse_ics(ICS_SAMPLE, "sample.ics", settings.timezone, window_start, window_end)
    titles = {event.title for event in events}
    assert titles == {"Standup", "All day offsite"}
    standup = next(event for event in events if event.title == "Standup")
    assert standup.start.strftime("%H:%M") == "09:30", "UTC 02:30 is 09:30 in Bangkok"
    assert standup.location == "Meet"
    assert standup.source == "ics"
    offsite = next(event for event in events if event.title == "All day offsite")
    assert offsite.all_day is True


def test_parse_ics_expands_a_recurring_event(settings: Settings) -> None:
    text = ICS_SAMPLE.replace(
        "LOCATION:Meet", "LOCATION:Meet\r\nRRULE:FREQ=DAILY;COUNT=3"
    )
    tz = zone(settings.timezone)
    events = parse_ics(
        text,
        "sample.ics",
        settings.timezone,
        datetime(2026, 9, 1, tzinfo=tz),
        datetime(2026, 9, 30, tzinfo=tz),
    )
    standups = [event for event in events if event.title == "Standup"]
    assert len(standups) == 3
    assert len({event.id for event in standups}) == 3


# -- weather ---------------------------------------------------------------
def test_fixture_weather_has_air_quality_and_forecast(settings: Settings) -> None:
    weather = run(FixtureWeatherAdapter(settings).fetch())
    assert weather.location_name == "Bangkok"
    assert weather.temperature_c is not None
    assert weather.feels_like_c is not None
    assert weather.pm2_5 is not None
    assert weather.aqi is not None
    assert len(weather.daily) == 5
    assert weather.daily[0].day == today_local(settings.timezone)
    assert weather.rain_from == "15:00"


def test_open_meteo_without_coordinates_is_unavailable(settings: Settings) -> None:
    with pytest.raises(AdapterUnavailable):
        run(OpenMeteoWeatherAdapter(settings).fetch())


def test_rain_window_finds_the_first_and_last_likely_hour(settings: Settings) -> None:
    tz = zone(settings.timezone)
    base = datetime(2026, 9, 4, 9, 0, tzinfo=tz)
    hourly = [
        HourlyRain(at=base + timedelta(hours=index), probability_percent=value)
        for index, value in enumerate([10, 20, 30, 55, 80, 70, 20])
    ]
    assert rain_window(hourly, base) == ("12:00", "14:00")


def test_rain_window_returns_nothing_when_it_stays_dry(settings: Settings) -> None:
    tz = zone(settings.timezone)
    base = datetime(2026, 9, 4, 9, 0, tzinfo=tz)
    hourly = [
        HourlyRain(at=base + timedelta(hours=index), probability_percent=5) for index in range(6)
    ]
    assert rain_window(hourly, base) == (None, None)


def test_weather_code_and_aqi_labels() -> None:
    assert condition_from_code(95) == "Thunderstorms"
    assert condition_from_code(None) == "unknown"
    assert condition_from_code(4242) == "unknown"
    assert aqi_label(30) == "Good"
    assert aqi_label(71) == "Moderate"
    assert aqi_label(None) is None


# -- ai usage --------------------------------------------------------------
def test_fixture_ai_usage_has_both_providers(settings: Settings) -> None:
    providers = run(FixtureAIUsageAdapter(settings).fetch())
    names = [provider.provider for provider in providers]
    assert names == ["Claude", "Codex"]
    claude = providers[0]
    assert claude.short_window_percent_remaining == 72
    assert claude.weekly_percent_remaining == 48
    assert claude.collection_status == "ok"
    # Never invented: Codex has no weekly number in the fixture.
    assert providers[1].weekly_percent_remaining is None


def test_file_ai_usage_reads_the_data_directory(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "ai-usage.json"
    target.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "Claude",
                        "short_window_percent_remaining": 10,
                        "collection_status": "ok",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    adapter = FileAIUsageAdapter(settings.model_copy(update={"ai_usage_path": target}))
    providers = run(adapter.fetch())
    assert providers[0].short_window_percent_remaining == 10


def test_file_ai_usage_missing_file_is_unavailable(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    adapter = FileAIUsageAdapter(
        settings.model_copy(update={"ai_usage_path": tmp_path / "nope.json"})
    )
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


# -- brief -----------------------------------------------------------------
def freeze_hour(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    """Pin the adapter's clock so the morning/evening switch is testable."""
    moment = datetime(2026, 9, 4, hour, 0, tzinfo=zone("Asia/Bangkok"))
    monkeypatch.setattr(ai_brief, "now_local", lambda timezone_name: moment)


def test_fixture_brief_picks_the_mode_by_local_hour(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_hour(monkeypatch, 8)
    assert run(FixtureBriefAdapter(settings).fetch()).mode is BriefMode.MORNING
    freeze_hour(monkeypatch, 20)
    assert run(FixtureBriefAdapter(settings).fetch()).mode is BriefMode.EVENING
    # The switch hour itself belongs to the evening.
    freeze_hour(monkeypatch, settings.brief_evening_hour)
    assert run(FixtureBriefAdapter(settings).fetch()).mode is BriefMode.EVENING


def test_fixture_brief_has_sections_and_a_note(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_hour(monkeypatch, 8)
    brief = run(FixtureBriefAdapter(settings).fetch())
    assert brief.headline
    assert brief.note
    assert len(brief.sections) == 5
    assert brief.sections[0].items


def test_markdown_brief_parser() -> None:
    text = (
        "# Two deadlines today\n"
        "Answer the vendor quote before standup.\n"
        "\n"
        "## Today's schedule\n"
        "- 09:30 standup\n"
        "- 11:00 design sync\n"
        "\n"
        "## At risk\n"
        "- Vendor quote overdue\n"
    )
    brief = parse_markdown_brief(text, BriefMode.MORNING)
    assert brief.headline == "Two deadlines today"
    assert brief.note == "Answer the vendor quote before standup."
    assert [section.title for section in brief.sections] == ["Today's schedule", "At risk"]
    assert brief.sections[0].items == ["09:30 standup", "11:00 design sync"]
    assert brief.source == "file"


def test_file_brief_reads_current_json(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    brief_dir = tmp_path / "brief"
    brief_dir.mkdir()
    (brief_dir / "current.json").write_text(
        json.dumps(
            {
                "mode": "morning",
                "headline": "From a file",
                "note": "Written by another agent",
                "sections": [{"title": "Key tasks", "items": ["Ship it"]}],
            }
        ),
        encoding="utf-8",
    )
    adapter = FileBriefAdapter(settings.model_copy(update={"brief_dir": brief_dir}))
    brief = run(adapter.fetch())
    assert brief.headline == "From a file"
    assert brief.generated_at is not None


def test_file_brief_falls_back_to_markdown(  # type: ignore[no-untyped-def]
    settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_hour(monkeypatch, 8)
    brief_dir = tmp_path / "brief-md"
    brief_dir.mkdir()
    (brief_dir / "morning.md").write_text(
        "# Morning\n\n## Key tasks\n- Ship it\n", encoding="utf-8"
    )
    adapter = FileBriefAdapter(settings.model_copy(update={"brief_dir": brief_dir}))
    brief = run(adapter.fetch())
    assert brief.mode is BriefMode.MORNING
    assert brief.sections[0].items == ["Ship it"]


def test_file_brief_missing_is_unavailable(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    adapter = FileBriefAdapter(settings.model_copy(update={"brief_dir": tmp_path / "empty"}))
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


# -- home assistant --------------------------------------------------------
def test_fixture_home_has_sensors_and_services(settings: Settings) -> None:
    home = run(FixtureHomeAdapter(settings).fetch())
    assert len(home.sensors) == 5
    assert len(home.services) == 7
    assert any(service.health is ServiceHealth.WARN for service in home.services)


def test_rest_home_without_credentials_is_unavailable(settings: Settings) -> None:
    with pytest.raises(AdapterUnavailable):
        run(RestHomeAdapter(settings).fetch())


def test_build_home_state_maps_entities() -> None:
    states = {
        "binary_sensor.front_door": {
            "entity_id": "binary_sensor.front_door",
            "state": "on",
            "attributes": {"device_class": "door"},
        },
        "sensor.office_temperature": {
            "entity_id": "sensor.office_temperature",
            "state": "27.4",
            "attributes": {"unit_of_measurement": "C"},
        },
        "binary_sensor.nas_online": {
            "entity_id": "binary_sensor.nas_online",
            "state": "off",
            "attributes": {},
        },
    }
    entity_map = {
        "front_door": "binary_sensor.front_door",
        "room_temperature": "sensor.office_temperature",
        "nas": "binary_sensor.nas_online",
        "proxmox": "binary_sensor.missing",
    }
    home = build_home_state(states, entity_map)
    door = next(sensor for sensor in home.sensors if sensor.key == "front_door")
    assert door.value == "Open"
    assert door.severity == "alert"
    temperature = next(sensor for sensor in home.sensors if sensor.key == "room_temperature")
    assert temperature.value == "27.4"
    assert temperature.unit == "C"
    nas = next(service for service in home.services if service.key == "nas")
    assert nas.health is ServiceHealth.DOWN
    missing = next(service for service in home.services if service.key == "proxmox")
    assert missing.health is ServiceHealth.UNKNOWN


# -- the safe wrapper ------------------------------------------------------
class _Boom:
    name = "boom"
    source = "test"

    def __init__(self, values: list[object]) -> None:
        self._values = values

    async def fetch(self) -> object:
        value = self._values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_cached_adapter_reports_unavailable_without_a_previous_value() -> None:
    cached = CachedAdapter(_Boom([AdapterUnavailable("not configured")]), ttl_seconds=60)
    outcome = run(cached.get())
    assert outcome.status is AdapterStatus.UNAVAILABLE
    assert outcome.value is None
    assert outcome.error == "not configured"


def test_cached_adapter_reports_error_without_a_previous_value() -> None:
    cached = CachedAdapter(_Boom([RuntimeError("network down")]), ttl_seconds=60)
    outcome = run(cached.get())
    assert outcome.status is AdapterStatus.ERROR
    assert outcome.value is None
    assert "network down" in (outcome.error or "")


def test_cached_adapter_keeps_the_last_good_value_when_a_fetch_fails() -> None:
    cached = CachedAdapter(_Boom(["good", RuntimeError("boom")]), ttl_seconds=0)
    first = run(cached.get())
    assert first.status is AdapterStatus.OK
    second = run(cached.get())
    assert second.status is AdapterStatus.STALE
    assert second.value == "good"


def test_cached_adapter_serves_from_the_ttl_cache() -> None:
    cached = CachedAdapter(_Boom(["first"]), ttl_seconds=600)
    assert run(cached.get()).value == "first"
    # The stub would raise IndexError on a second fetch, so this must be cached.
    assert run(cached.get()).value == "first"


def test_cached_adapter_force_bypasses_the_cache() -> None:
    cached = CachedAdapter(_Boom(["first", "second"]), ttl_seconds=600)
    assert run(cached.get()).value == "first"
    assert run(cached.get(force=True)).value == "second"
