"""Fixture adapters, push adapters, failure handling and the derived weather
helpers."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest

import httpx

from app.adapters import ai_brief
from app.adapters import calendar as calendar_module
from app.adapters.ai_brief import FixtureBriefAdapter, PushBriefAdapter
from app.adapters.ai_usage import FixtureAIUsageAdapter, PushAIUsageAdapter
from app.adapters.base import AdapterError, AdapterUnavailable, CachedAdapter
from app.adapters.calendar import FixtureCalendarAdapter, IcsCalendarAdapter, parse_ics
from app.adapters.home_assistant import FixtureHomeAdapter, RestHomeAdapter, build_home_state
from app.adapters.tasks import FixtureTasksAdapter, ObsidianTasksAdapter, PushTasksAdapter
from app.adapters.weather import (
    FixtureWeatherAdapter,
    OpenMeteoWeatherAdapter,
    aqi_label,
    condition_from_code,
    rain_window,
)
from app.config import Env
from app.datasets import write_dataset
from app.db import get_database
from app.models import AdapterStatus, BriefMode, HourlyRain, Priority, ServiceHealth
from app.modules.agenda import FIXTURE as CALENDAR_FIXTURE
from app.modules.ai_usage import FIXTURE as AI_USAGE_FIXTURE
from app.modules.brief import FIXTURE as BRIEF_FIXTURE
from app.modules.calendar.settings import Feed
from app.modules.system import HOME_FIXTURE
from app.modules.tasks import FIXTURE as TASKS_FIXTURE
from app.modules.weather import FIXTURE as WEATHER_FIXTURE
from app.settings import HubSettings
from app.timeutil import today_local, zone
from tests.conftest import run


def _push_env(env: Env, tmp_path) -> Env:  # type: ignore[no-untyped-def]
    """An ``Env`` with its own isolated database, for a push adapter test:
    ``hub_db_file`` is derived from ``data_dir``, so a fresh ``tmp_path``
    gives every such test its own ``deskmate.sqlite`` rather than sharing
    the session-scoped ``env`` fixture's one."""
    return env.model_copy(update={"data_dir": tmp_path})


# -- tasks -----------------------------------------------------------------
def test_fixture_tasks_are_shifted_to_today(hub_settings: HubSettings, env: Env) -> None:
    tasks = run(FixtureTasksAdapter(hub_settings.tasks, hub_settings.general, env, TASKS_FIXTURE).fetch())
    assert tasks
    today = today_local(hub_settings.general.timezone)
    dues = {task.due for task in tasks if task.due is not None}
    assert today in dues, "the anchor task should land on today"
    assert any(task.due is not None and task.due < today for task in tasks), "one overdue task"
    assert {task.source for task in tasks} == {"fixture"}
    assert any(task.priority is Priority.HIGH for task in tasks)
    assert any(task.completed for task in tasks)


def test_fixture_tasks_keep_literal_dates_when_shifting_is_off(
    hub_settings: HubSettings, env: Env
) -> None:
    literal = env.model_copy(update={"fixture_relative_dates": False})
    tasks = run(FixtureTasksAdapter(hub_settings.tasks, hub_settings.general, literal, TASKS_FIXTURE).fetch())
    assert any(task.due == date(2026, 9, 4) for task in tasks)


def test_obsidian_adapter_without_a_vault_is_unavailable(hub_settings: HubSettings, env: Env) -> None:
    tasks = hub_settings.tasks.model_copy(update={"obsidian_vault_path": None})
    adapter = ObsidianTasksAdapter(tasks, env)
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


def test_push_tasks_is_unavailable_before_anything_is_pushed(  # type: ignore[no-untyped-def]
    hub_settings: HubSettings, env: Env, tmp_path
) -> None:
    local = _push_env(env, tmp_path)
    adapter = PushTasksAdapter(hub_settings.tasks, hub_settings.general, local)
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


def test_push_tasks_round_trips_with_no_date_shifting(  # type: ignore[no-untyped-def]
    hub_settings: HubSettings, env: Env, tmp_path
) -> None:
    local = _push_env(env, tmp_path)
    database = get_database(local.hub_db_file)
    database.migrate()
    received_at = datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc)
    write_dataset(
        database,
        "tasks",
        {"tasks": [{"id": "agent-1", "title": "Ship it", "due": "2026-09-04", "priority": "high"}]},
        received_at,
    )
    adapter = PushTasksAdapter(hub_settings.tasks, hub_settings.general, local)
    tasks = run(adapter.fetch())
    assert len(tasks) == 1
    assert tasks[0].id == "agent-1"
    # No date shifting: the literal due date survives even though the
    # fixture-relative-dates switch is on.
    assert tasks[0].due == date(2026, 9, 4)
    assert tasks[0].source == "push"
    assert adapter.last_received_at is not None


# -- calendar --------------------------------------------------------------
def test_fixture_calendar_is_sorted_and_localized(hub_settings: HubSettings, env: Env) -> None:
    events = run(FixtureCalendarAdapter(hub_settings.calendar, hub_settings.general, env, CALENDAR_FIXTURE).fetch())
    assert events
    assert events == sorted(events, key=lambda event: (event.start, event.title))
    assert all(event.start.tzinfo is not None for event in events)
    assert any(event.all_day for event in events)


def test_ics_adapter_without_urls_is_unavailable(hub_settings: HubSettings, env: Env) -> None:
    with pytest.raises(AdapterUnavailable):
        run(IcsCalendarAdapter(hub_settings.calendar, hub_settings.general, env).fetch())


def test_a_failing_feed_names_itself_but_never_leaks_its_url(
    hub_settings: HubSettings, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Google/Outlook ICS URL's "secret address" is a credential: a 404
    from the feed must not put the URL into the adapter's error string,
    which is readable through Outcome.error, /healthz and /api/state."""
    secret_url = "https://calendar.google.com/calendar/ical/super-secret-address/basic.ics"
    calendar = hub_settings.calendar.model_copy(
        update={"source": "ics", "feeds": [Feed(url=secret_url, name="Work")]}
    )

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(self, url: str, follow_redirects: bool = True) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(404, request=request)

    monkeypatch.setattr(calendar_module.httpx, "AsyncClient", _FakeClient)

    with pytest.raises(AdapterError) as excinfo:
        run(IcsCalendarAdapter(calendar, hub_settings.general, env).fetch())

    message = str(excinfo.value)
    assert secret_url not in message
    assert "Work" in message
    assert "404" in message


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


def test_parse_ics_reads_timed_and_all_day_events(hub_settings: HubSettings) -> None:
    timezone_name = hub_settings.general.timezone
    tz = zone(timezone_name)
    window_start = datetime(2026, 9, 1, tzinfo=tz)
    window_end = datetime(2026, 9, 30, tzinfo=tz)
    events = parse_ics(ICS_SAMPLE, "sample.ics", timezone_name, window_start, window_end)
    titles = {event.title for event in events}
    assert titles == {"Standup", "All day offsite"}
    standup = next(event for event in events if event.title == "Standup")
    assert standup.start.strftime("%H:%M") == "09:30", "UTC 02:30 is 09:30 in Bangkok"
    assert standup.location == "Meet"
    assert standup.source == "ics"
    offsite = next(event for event in events if event.title == "All day offsite")
    assert offsite.all_day is True


def test_parse_ics_expands_a_recurring_event(hub_settings: HubSettings) -> None:
    text = ICS_SAMPLE.replace(
        "LOCATION:Meet", "LOCATION:Meet\r\nRRULE:FREQ=DAILY;COUNT=3"
    )
    timezone_name = hub_settings.general.timezone
    tz = zone(timezone_name)
    events = parse_ics(
        text,
        "sample.ics",
        timezone_name,
        datetime(2026, 9, 1, tzinfo=tz),
        datetime(2026, 9, 30, tzinfo=tz),
    )
    standups = [event for event in events if event.title == "Standup"]
    assert len(standups) == 3
    assert len({event.id for event in standups}) == 3


# -- weather ---------------------------------------------------------------
def test_fixture_weather_has_air_quality_and_forecast(hub_settings: HubSettings, env: Env) -> None:
    weather = run(FixtureWeatherAdapter(hub_settings.weather, hub_settings.general, env, WEATHER_FIXTURE).fetch())
    assert weather.location_name == "Bangkok"
    assert weather.temperature_c is not None
    assert weather.feels_like_c is not None
    assert weather.pm2_5 is not None
    assert weather.aqi is not None
    assert len(weather.daily) == 5
    assert weather.daily[0].day == today_local(hub_settings.general.timezone)
    assert weather.rain_from == "15:00"


def test_open_meteo_without_coordinates_is_unavailable(hub_settings: HubSettings, env: Env) -> None:
    with pytest.raises(AdapterUnavailable):
        run(OpenMeteoWeatherAdapter(hub_settings.weather, hub_settings.general, env).fetch())


def test_rain_window_finds_the_first_and_last_likely_hour(hub_settings: HubSettings) -> None:
    tz = zone(hub_settings.general.timezone)
    base = datetime(2026, 9, 4, 9, 0, tzinfo=tz)
    hourly = [
        HourlyRain(at=base + timedelta(hours=index), probability_percent=value)
        for index, value in enumerate([10, 20, 30, 55, 80, 70, 20])
    ]
    assert rain_window(hourly, base) == ("12:00", "14:00")


def test_rain_window_returns_nothing_when_it_stays_dry(hub_settings: HubSettings) -> None:
    tz = zone(hub_settings.general.timezone)
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
def test_fixture_ai_usage_has_both_providers(hub_settings: HubSettings, env: Env) -> None:
    providers = run(FixtureAIUsageAdapter(hub_settings.ai_usage, hub_settings.general, env, AI_USAGE_FIXTURE).fetch())
    names = [provider.provider for provider in providers]
    assert names == ["Claude", "Codex"]
    claude = providers[0]
    assert claude.short_window_percent_remaining == 72
    assert claude.weekly_percent_remaining == 48
    assert claude.collection_status == "ok"
    # Never invented: Codex has no weekly number in the fixture.
    assert providers[1].weekly_percent_remaining is None


def test_push_ai_usage_reads_the_dataset_row(  # type: ignore[no-untyped-def]
    hub_settings: HubSettings, env: Env, tmp_path
) -> None:
    local = _push_env(env, tmp_path)
    database = get_database(local.hub_db_file)
    database.migrate()
    write_dataset(
        database,
        "ai_usage",
        {
            "providers": [
                {
                    "provider": "Claude",
                    "short_window_percent_remaining": 10,
                    "collection_status": "ok",
                }
            ]
        },
    )
    adapter = PushAIUsageAdapter(hub_settings.ai_usage, hub_settings.general, local)
    providers = run(adapter.fetch())
    assert providers[0].short_window_percent_remaining == 10
    assert adapter.last_received_at is not None


def test_push_ai_usage_is_unavailable_before_anything_is_pushed(  # type: ignore[no-untyped-def]
    hub_settings: HubSettings, env: Env, tmp_path
) -> None:
    local = _push_env(env, tmp_path)
    adapter = PushAIUsageAdapter(hub_settings.ai_usage, hub_settings.general, local)
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


# -- brief -----------------------------------------------------------------
def freeze_hour(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    """Pin the adapter's clock so the morning/evening switch is testable."""
    moment = datetime(2026, 9, 4, hour, 0, tzinfo=zone("Asia/Bangkok"))
    monkeypatch.setattr(ai_brief, "now_local", lambda timezone_name: moment)


def test_fixture_brief_picks_the_mode_by_local_hour(
    hub_settings: HubSettings, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FixtureBriefAdapter(hub_settings.brief, hub_settings.general, env, BRIEF_FIXTURE)
    freeze_hour(monkeypatch, 8)
    assert run(adapter.fetch()).mode is BriefMode.MORNING
    freeze_hour(monkeypatch, 20)
    assert run(adapter.fetch()).mode is BriefMode.EVENING
    # The switch hour itself belongs to the evening.
    freeze_hour(monkeypatch, hub_settings.brief.evening_hour)
    assert run(adapter.fetch()).mode is BriefMode.EVENING


def test_fixture_brief_has_sections_and_a_note(
    hub_settings: HubSettings, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_hour(monkeypatch, 8)
    brief = run(FixtureBriefAdapter(hub_settings.brief, hub_settings.general, env, BRIEF_FIXTURE).fetch())
    assert brief.headline
    assert brief.note
    assert len(brief.sections) == 5
    assert brief.sections[0].items


def test_push_brief_reads_the_dataset_row(  # type: ignore[no-untyped-def]
    hub_settings: HubSettings, env: Env, tmp_path
) -> None:
    local = _push_env(env, tmp_path)
    database = get_database(local.hub_db_file)
    database.migrate()
    write_dataset(
        database,
        "brief",
        {
            "mode": "morning",
            "headline": "From a push",
            "note": "Written by another agent",
            "sections": [{"title": "Key tasks", "items": ["Ship it"]}],
        },
    )
    adapter = PushBriefAdapter(hub_settings.brief, hub_settings.general, local)
    brief = run(adapter.fetch())
    assert brief.headline == "From a push"
    assert brief.source == "push"
    assert adapter.last_received_at is not None


def test_push_brief_is_shown_regardless_of_the_current_mode(  # type: ignore[no-untyped-def]
    hub_settings: HubSettings, env: Env, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike the fixture, a pushed brief is not mode-selected: whatever was
    last pushed is shown even after the clock crosses the evening hour."""
    freeze_hour(monkeypatch, 20)
    local = _push_env(env, tmp_path)
    database = get_database(local.hub_db_file)
    database.migrate()
    write_dataset(database, "brief", {"mode": "morning", "headline": "Still morning"})
    adapter = PushBriefAdapter(hub_settings.brief, hub_settings.general, local)
    brief = run(adapter.fetch())
    assert brief.headline == "Still morning"
    assert brief.mode is BriefMode.MORNING


def test_push_brief_is_unavailable_before_anything_is_pushed(  # type: ignore[no-untyped-def]
    hub_settings: HubSettings, env: Env, tmp_path
) -> None:
    local = _push_env(env, tmp_path)
    adapter = PushBriefAdapter(hub_settings.brief, hub_settings.general, local)
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


# -- home assistant --------------------------------------------------------
def test_fixture_home_has_sensors_and_services(hub_settings: HubSettings, env: Env) -> None:
    home = run(FixtureHomeAdapter(hub_settings.home, env, HOME_FIXTURE).fetch())
    assert len(home.sensors) == 5
    assert len(home.services) == 7
    assert any(service.health is ServiceHealth.WARN for service in home.services)


def test_rest_home_without_credentials_is_unavailable(hub_settings: HubSettings, env: Env) -> None:
    with pytest.raises(AdapterUnavailable):
        run(RestHomeAdapter(hub_settings.home, env).fetch())


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
