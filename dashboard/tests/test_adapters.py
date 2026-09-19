"""Fixture adapters, failure handling and the derived weather helpers."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta

import pytest

from app.adapters import ai_brief
from app.adapters.ai_brief import (
    AutoBriefAdapter,
    FileBriefAdapter,
    FixtureBriefAdapter,
    parse_markdown_brief,
)
from app.adapters.ai_usage import AutoAIUsageAdapter, FileAIUsageAdapter, FixtureAIUsageAdapter
from app.adapters.base import AdapterUnavailable, CachedAdapter
from app.adapters.calendar import FixtureCalendarAdapter, IcsCalendarAdapter, parse_ics
from app.adapters.home_assistant import FixtureHomeAdapter, RestHomeAdapter, build_home_state
from app.adapters.tasks import (
    AutoTasksAdapter,
    FileTasksAdapter,
    FixtureTasksAdapter,
    ObsidianTasksAdapter,
)
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


def test_file_tasks_round_trips_with_no_date_shifting(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "tasks.json"
    target.write_text(
        json.dumps(
            {
                "received_at": "2026-09-04T08:00:00+00:00",
                "tasks": [
                    {"id": "agent-1", "title": "Ship it", "due": "2026-09-04", "priority": "high"}
                ],
            }
        ),
        encoding="utf-8",
    )
    local = settings.model_copy(update={"data_dir": tmp_path, "fixture_relative_dates": True})
    adapter = FileTasksAdapter(local)
    tasks = run(adapter.fetch())
    assert len(tasks) == 1
    assert tasks[0].id == "agent-1"
    # No date shifting: the literal due date survives even though the
    # fixture-relative-dates switch is on.
    assert tasks[0].due == date(2026, 9, 4)
    assert tasks[0].source == "file"
    assert adapter.last_received_at is not None


def test_file_tasks_falls_back_to_mtime_without_a_received_at_key(  # type: ignore[no-untyped-def]
    settings: Settings, tmp_path
) -> None:
    target = tmp_path / "tasks.json"
    target.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    adapter = FileTasksAdapter(settings.model_copy(update={"data_dir": tmp_path}))
    run(adapter.fetch())
    assert adapter.last_received_at is not None


def test_file_tasks_missing_file_is_unavailable(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    adapter = FileTasksAdapter(settings.model_copy(update={"data_dir": tmp_path}))
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


def test_auto_tasks_picks_fixture_before_a_push_and_file_after(  # type: ignore[no-untyped-def]
    settings: Settings, tmp_path
) -> None:
    local = settings.model_copy(update={"data_dir": tmp_path})
    adapter = AutoTasksAdapter(local)
    assert adapter.source == "fixture"
    tasks_before = run(adapter.fetch())
    assert {task.source for task in tasks_before} == {"fixture"}
    assert adapter.source == "fixture"

    (tmp_path / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "a", "title": "Pushed"}]}), encoding="utf-8"
    )
    # source reports the delegate used on the *last fetch*, not a live stat:
    # writing the file alone does not flip it until fetch() runs again.
    assert adapter.source == "fixture"
    tasks_after = run(adapter.fetch())
    assert [task.title for task in tasks_after] == ["Pushed"]
    assert adapter.source == "file"


def test_auto_tasks_source_survives_the_file_being_deleted_until_the_next_fetch(  # type: ignore[no-untyped-def]
    settings: Settings, tmp_path
) -> None:
    local = settings.model_copy(update={"data_dir": tmp_path})
    adapter = AutoTasksAdapter(local)
    (tmp_path / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    run(adapter.fetch())
    assert adapter.source == "file"

    (tmp_path / "tasks.json").unlink()
    # Still "file": nothing has re-fetched yet (that is CachedAdapter's TTL
    # or invalidate() to trigger, not this adapter's job).
    assert adapter.source == "file"


def test_auto_tasks_falls_back_to_fixture_when_the_file_vanishes_mid_fetch(  # type: ignore[no-untyped-def]
    settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: is_file() says yes, but the file is gone by the time the
    file adapter actually opens it (deleted between the check and the read).
    auto must fall back to fixture, not surface an error block."""
    local = settings.model_copy(update={"data_dir": tmp_path})
    adapter = AutoTasksAdapter(local)
    (tmp_path / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")

    real_fetch = adapter._file.fetch

    async def vanish_then_fetch() -> list[Task]:
        (tmp_path / "tasks.json").unlink()
        return await real_fetch()

    monkeypatch.setattr(adapter._file, "fetch", vanish_then_fetch)
    tasks = run(adapter.fetch())
    assert tasks  # fixture tasks, not an empty/error result
    assert adapter.source == "fixture"


def test_auto_ai_usage_picks_fixture_before_and_file_after(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    local = settings.model_copy(update={"ai_usage_path": tmp_path / "ai-usage.json"})
    adapter = AutoAIUsageAdapter(local)
    assert adapter.source == "fixture"
    (tmp_path / "ai-usage.json").write_text(
        json.dumps({"providers": [{"provider": "Claude"}]}), encoding="utf-8"
    )
    assert adapter.source == "fixture"
    providers = run(adapter.fetch())
    assert providers[0].provider == "Claude"
    assert adapter.source == "file"


def test_auto_brief_picks_fixture_before_and_file_after(  # type: ignore[no-untyped-def]
    settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_hour(monkeypatch, 8)
    brief_dir = tmp_path / "brief-auto"
    local = settings.model_copy(update={"brief_dir": brief_dir})
    adapter = AutoBriefAdapter(local)
    assert adapter.source == "fixture"
    brief_dir.mkdir()
    (brief_dir / "current.json").write_text(
        json.dumps({"headline": "Pushed brief", "sections": []}), encoding="utf-8"
    )
    assert adapter.source == "fixture"
    brief = run(adapter.fetch())
    assert brief.headline == "Pushed brief"
    assert adapter.source == "file"


def test_auto_brief_serves_the_fixture_when_only_the_other_mode_markdown_exists(  # type: ignore[no-untyped-def]
    settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only morning.md is present but the clock says evening: FileBriefAdapter
    would raise AdapterUnavailable (it only ever reads the current mode's own
    file), so auto must serve the fixture, not an error block."""
    freeze_hour(monkeypatch, 20)
    brief_dir = tmp_path / "brief-mismatch"
    brief_dir.mkdir()
    (brief_dir / "morning.md").write_text("# Morning\n\n## Key tasks\n- Ship it\n", encoding="utf-8")
    local = settings.model_copy(update={"brief_dir": brief_dir})
    adapter = AutoBriefAdapter(local)
    assert adapter._file_available() is False
    brief = run(adapter.fetch())
    assert brief.source == "fixture"
    assert adapter.source == "fixture"


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


def test_failed_fetch_is_replayed_within_the_backoff() -> None:
    cached = CachedAdapter(_Boom([RuntimeError("down"), "good"]), ttl_seconds=600)
    first = run(cached.get())
    assert first.status is AdapterStatus.ERROR
    # The stub would return "good" on a second fetch; a replayed failure
    # instead of a refetch means this is still the error, value None.
    second = run(cached.get())
    assert second.status is AdapterStatus.ERROR
    assert second.value is None


def test_invalidate_clears_the_failure_backoff() -> None:
    cached = CachedAdapter(_Boom([RuntimeError("down"), "good"]), ttl_seconds=600)
    run(cached.get())
    cached.invalidate()
    outcome = run(cached.get())
    assert outcome.status is AdapterStatus.OK
    assert outcome.value == "good"


def test_force_bypasses_the_failure_backoff() -> None:
    cached = CachedAdapter(_Boom([RuntimeError("down"), "good"]), ttl_seconds=600)
    run(cached.get())
    outcome = run(cached.get(force=True))
    assert outcome.status is AdapterStatus.OK
    assert outcome.value == "good"


class _Slow:
    """A fetch that blocks until the test lets it finish, so a test can
    invalidate() while it is in flight. A value that is an Exception
    instance is raised instead of returned, mirroring _Boom."""

    name = "slow"
    source = "test"

    def __init__(self, values: list[object]) -> None:
        self._values = values
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def fetch(self) -> object:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        value = self._values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_invalidate_during_a_fetch_forces_the_next_get_to_refetch() -> None:
    async def scenario() -> None:
        adapter = _Slow(["stale", "fresh"])
        cached = CachedAdapter(adapter, ttl_seconds=600)
        task = asyncio.create_task(cached.get())
        await adapter.started.wait()
        # A push writes its file and calls invalidate() while the get()
        # above is still awaiting its (slow) fetch of the old file.
        cached.invalidate()
        adapter.release.set()
        first = await task
        assert first.value == "stale"
        assert adapter.calls == 1

        # The in-flight fetch must not have resurrected the cache: this
        # get() has to fetch again, not serve "stale" from the TTL cache.
        second = await cached.get()
        assert second.value == "fresh"
        assert adapter.calls == 2

    run(scenario())


def test_invalidate_during_a_failing_fetch_is_not_masked_by_the_backoff() -> None:
    async def scenario() -> None:
        adapter = _Slow([RuntimeError("down"), "fresh"])
        cached = CachedAdapter(adapter, ttl_seconds=600)
        task = asyncio.create_task(cached.get())
        await adapter.started.wait()
        # A push landing while this failing fetch is in flight must not get
        # hidden behind a 60s backoff it never asked for.
        cached.invalidate()
        adapter.release.set()
        first = await task
        assert first.status is AdapterStatus.ERROR
        assert adapter.calls == 1

        second = await cached.get()
        assert second.value == "fresh"
        assert adapter.calls == 2

    run(scenario())
