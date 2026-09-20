"""The settings page, the section saves, "Save and test", the setup wizard
and the place search.

Every test builds its own app over its own ``DATA_DIR``: a section save calls
``Hub.reload()``, which re-reads the settings snapshot from the database, so
none of this can run against the shared session ``client`` without changing
what the rest of the suite renders.

Most tests use ``TestClient(app)`` **without** the ``with`` block, so the
lifespan never runs and no Chromium starts: they only render HTML, and the
Jinja environment those pages use is built in ``Renderer.__init__``. The one
test that compares two rendered PNGs takes the lifespan, and so the browser,
deliberately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app import geocode
from app.config import REPO_ROOT, Env
from app.geocode import GEOCODING_URL, MAX_QUERY_LENGTH, RESULT_COUNT, SEARCH_FAILED
from app.hub_config import COOKIE_NAME, ClaimedSecrets
from app.main import create_app
from app.models import AdapterStatus, TasksBlock
from app.modules.registry import ModulesSettings, ModuleToggle
from app.settings import SECTIONS, HubSettings
from app.settings_pages import WIZARD_STEPS
from app.view import build_context
from tests.conftest import make_state

FIXTURES_DIR = REPO_ROOT / "fixtures"

#: The general section as a browser submits it, unchanged.
GENERAL_FORM = {"timezone": "Asia/Bangkok", "units": "metric", "action": "save"}

#: The weather section as a browser submits it, source swapped to the demo
#: data so a render changes without reaching the network.
WEATHER_FIXTURE_FORM = {
    "source": "fixture",
    "latitude": "",
    "longitude": "",
    "location_name": "",
    "ttl_seconds": "900",
    "action": "save",
}


def env_for(data_dir: Path) -> Env:
    return Env(
        _env_file=None,
        DATA_DIR=data_dir,
        FIXTURES_DIR=FIXTURES_DIR,
        LOG_LEVEL="WARNING",
    )


class AdminHub:
    """A claimed hub with its own database, and an admin bearer token."""

    def __init__(self, data_dir: Path) -> None:
        self.env = env_for(data_dir)
        self.app = create_app(self.env, HubSettings())
        self.hub = self.app.state.hub
        self.client = TestClient(self.app)
        self.secrets: ClaimedSecrets = asyncio.run(
            self.hub.identity.claim(name="deskmate", base_url="http://dashboard-hub.lan:8080")
        )

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secrets.token}"}

    def reader_client(self) -> TestClient:
        """A browser signed in with the device key: a reader, never an admin."""
        response = self.client.post(
            "/login", data={"key": self.secrets.device_key, "next": "/preview"},
            follow_redirects=False,
        )
        cookie = response.cookies.get(COOKIE_NAME)
        assert cookie
        return TestClient(self.app, cookies={COOKIE_NAME: str(cookie)})


@pytest.fixture()
def admin(tmp_path: Path) -> Iterator[AdminHub]:
    hub = AdminHub(tmp_path / "live")
    yield hub
    hub.hub.db.close()


def state_at(hour: int = 9) -> Any:
    """A state with no blocks at all: every page draws placeholders.

    Enough for the footer and the "unavailable" rules below, and it never
    runs an adapter, so these tests neither reach the network nor depend on
    what a fixture happens to hold.
    """
    return make_state(
        generated_at=datetime(2026, 9, 20, hour, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    )


def window_names(admin: AdminHub) -> list[str]:
    """The footer's window list, as the panel would print it."""
    context = build_context(
        "today", state_at(), admin.hub.hub_settings, admin.hub.registry.pages()
    )
    return [window["name"] for window in context["footer"]["windows"]]


def modules_form(
    admin: AdminHub,
    *,
    disable: frozenset[str] = frozenset(),
    extra_rows: tuple[str, ...] = (),
) -> dict[str, list[str]]:
    """The modules section exactly as the browser posts it.

    Built the way ``_section_form.html`` builds the HTML - a hidden ``0``
    before every checkbox, a ticked box adding its ``1`` after it, which is
    why the enabled fields carry two values - from the rows the page is
    showing, so this is a round trip through the page rather than through a
    hand-made payload.
    """
    items: dict[str, list[str]] = {}
    rows = [(row.id, row.enabled, row.order) for row in admin.hub.registry.toggle_rows()]
    rows.extend((module_id, True, None) for module_id in extra_rows)
    for index, (module_id, enabled, order) in enumerate(rows):
        items[f"items-{index}-id"] = [module_id]
        ticked = enabled and module_id not in disable
        items[f"items-{index}-enabled"] = ["0", "1"] if ticked else ["0"]
        items[f"items-{index}-order"] = ["" if order is None else str(order)]
    items["action"] = ["save"]
    return items


def save_modules(admin: AdminHub, **kwargs: Any) -> Any:
    return admin.client.post(
        "/settings/modules",
        data=modules_form(admin, **kwargs),
        headers=admin.auth,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
def test_the_page_carries_every_section_then_backup_and_the_danger_zone(
    admin: AdminHub,
) -> None:
    page = admin.client.get("/settings", headers=admin.auth)

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    for section in SECTIONS:
        assert f'id="{section}"' in page.text
        assert f'action="/settings/{section}"' in page.text
    # The 1.5 sections stay below the generated ones.
    assert page.text.index('action="/settings/alert"') < page.text.index(
        'action="/settings/backup"'
    )
    assert "Danger zone" in page.text
    # No JavaScript anywhere on the admin pages.
    assert "<script" not in page.text
    # Nine sections, seven of them with a field called source: the ids have
    # to be unique down the page or a label focuses the wrong input.
    assert page.text.count('id="weather-source"') == 1
    assert page.text.count('id="tasks-source"') == 1
    assert 'id="source"' not in page.text


def test_save_and_test_is_offered_only_where_there_is_a_source(admin: AdminHub) -> None:
    page = admin.client.get("/settings", headers=admin.auth)
    general = page.text[page.text.index('id="general"') : page.text.index('id="tasks"')]
    weather = page.text[page.text.index('id="weather"') : page.text.index('id="ai_usage"')]

    assert 'value="test"' not in general
    assert 'value="test"' in weather


# ---------------------------------------------------------------------------
# saving
# ---------------------------------------------------------------------------
def test_saving_a_section_stores_it_reloads_the_hub_and_says_so(admin: AdminHub) -> None:
    response = admin.client.post(
        "/settings/general",
        headers=admin.auth,
        data={"timezone": "Europe/Berlin", "units": "imperial", "action": "save"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=general#general"
    # The row is written and the live snapshot every page reads is rebuilt.
    assert admin.hub.settings_store.load("general").timezone == "Europe/Berlin"
    assert admin.hub.hub_settings.general.timezone == "Europe/Berlin"
    assert admin.hub.hub_settings.general.units == "imperial"

    page = admin.client.get("/settings?saved=general", headers=admin.auth)
    assert "Saved." in page.text
    assert 'value="Europe/Berlin"' in page.text


def test_alert_default_duration_is_used_when_a_post_omits_its_own(admin: AdminHub) -> None:
    """The ``alert`` section's ``default_duration_seconds`` (settings-modules
    gap 1): a ``POST /api/alert`` body without ``duration_seconds`` must use
    whatever was last saved there, not the hardcoded 90 the model used to
    fall back to."""
    saved = admin.client.post(
        "/settings/alert",
        headers=admin.auth,
        data={"default_duration_seconds": "150", "action": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert admin.hub.hub_settings.alert.default_duration_seconds == 150

    created = admin.client.post(
        "/api/alert", headers=admin.auth, json={"title": "Laundry done"}
    )
    assert created.status_code == 201
    assert created.json()["alert"]["duration_seconds"] == 150

    state = admin.client.get("/api/state", headers=admin.auth).json()
    assert state["alert"]["duration_seconds"] == 150


def test_alert_default_duration_out_of_bounds_is_422(admin: AdminHub) -> None:
    """``default_duration_seconds`` has to stay inside the same window as
    ``AlertRequest.duration_seconds`` (models.py, ge=5, le=600): main.py
    injects this value with ``model_copy``, which skips re-validation."""
    response = admin.client.post(
        "/settings/alert",
        headers=admin.auth,
        data={"default_duration_seconds": "0", "action": "save"},
    )

    assert response.status_code == 422
    assert admin.hub.hub_settings.alert.default_duration_seconds == 90


def test_a_bad_timezone_comes_back_on_the_field_and_saves_nothing(admin: AdminHub) -> None:
    response = admin.client.post(
        "/settings/general",
        headers=admin.auth,
        data={"timezone": "Mars/Olympus", "units": "metric", "action": "save"},
    )

    assert response.status_code == 422
    # The message sits next to the input, and the input still holds what was
    # typed: nobody retypes a form because one field was wrong.
    field_error = response.text.index('class="field-error"')
    assert 'value="Mars/Olympus"' in response.text[:field_error]
    assert "unknown timezone" in response.text[field_error : field_error + 200]
    assert admin.hub.settings_store.load("general").timezone == "Asia/Bangkok"
    assert admin.hub.hub_settings.general.timezone == "Asia/Bangkok"


def test_a_too_long_timezone_is_422_not_a_500(admin: AdminHub) -> None:
    """``ZoneInfo()`` looks a name up on the filesystem, and a name over 255
    bytes is not a legal filename: this used to escape as a 500."""
    response = admin.client.post(
        "/settings/general",
        headers=admin.auth,
        data={"timezone": "a" * 300, "units": "metric", "action": "save"},
    )

    assert response.status_code == 422
    assert 'class="field-error"' in response.text
    assert admin.hub.hub_settings.general.timezone == "Asia/Bangkok"


def test_a_timezone_with_an_illegal_filename_character_is_422_not_a_500(
    admin: AdminHub,
) -> None:
    """"<" is not a legal Windows filename character: ``ZoneInfo()`` raises
    ``OSError`` rather than ``ZoneInfoNotFoundError`` for it."""
    response = admin.client.post(
        "/settings/general",
        headers=admin.auth,
        data={"timezone": "<script>", "units": "metric", "action": "save"},
    )

    assert response.status_code == 422
    field_error = response.text.index('class="field-error"')
    assert "unknown timezone" in response.text[field_error : field_error + 200]
    assert admin.hub.hub_settings.general.timezone == "Asia/Bangkok"


def test_a_secret_left_blank_survives_a_save_of_its_section(admin: AdminHub) -> None:
    first = admin.client.post(
        "/settings/home",
        headers=admin.auth,
        data={
            "source": "rest",
            "url": "http://home.lan:8123",
            "token": "ha-token-value",
            "ttl_seconds": "120",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = admin.client.post(
        "/settings/home",
        headers=admin.auth,
        data={
            "source": "rest",
            "url": "http://home.lan:8124",
            "token": "",
            "ttl_seconds": "120",
            "action": "save",
        },
        follow_redirects=False,
    )

    assert second.status_code == 303
    stored = admin.hub.settings_store.load("home")
    assert stored.url == "http://home.lan:8124"
    assert stored.token.get_secret_value() == "ha-token-value"
    # And the page never echoes it back.
    page = admin.client.get("/settings", headers=admin.auth)
    assert "ha-token-value" not in page.text


def test_a_nan_ttl_is_422(admin: AdminHub) -> None:
    """Every ``float`` settings field is ``allow_inf_nan=False``: a NaN TTL
    would otherwise sail through validation and wreck any comparison the
    caching code does against it."""
    response = admin.client.post(
        "/settings/weather",
        headers=admin.auth,
        data={
            "source": "open_meteo",
            "latitude": "",
            "longitude": "",
            "location_name": "",
            "ttl_seconds": "nan",
            "action": "save",
        },
    )

    assert response.status_code == 422
    assert admin.hub.hub_settings.weather.ttl_seconds != float("nan")


def test_agenda_days_of_zero_is_422(admin: AdminHub) -> None:
    response = admin.client.post(
        "/settings/calendar",
        headers=admin.auth,
        data={"source": "ics", "agenda_days": "0", "ttl_seconds": "300", "action": "save"},
    )

    assert response.status_code == 422
    assert admin.hub.hub_settings.calendar.agenda_days == 7


def test_an_unknown_section_is_404(admin: AdminHub) -> None:
    assert admin.client.post("/settings/nope", headers=admin.auth, data={}).status_code == 404


def test_the_literal_database_routes_still_win_over_the_section_route(
    admin: AdminHub,
) -> None:
    """``/settings/{section}`` is declared last on purpose: if it matched
    first, the backup download would be parsed as a settings section."""
    response = admin.client.post("/settings/rotate", headers=admin.auth, data={})
    assert response.status_code == 422
    assert "confirmation box" in response.text


def test_saving_a_source_changes_the_rendered_page(admin: AdminHub) -> None:
    """The point of the reload: the next render uses what was just saved.

    This is the one test here that takes the lifespan, and so a browser: a
    PNG is the only honest proof that a settings save reached the renderer.
    """
    with TestClient(admin.app) as client:
        before = client.get("/display/weather.png", headers=admin.auth)
        assert before.status_code == 200

        saved = client.post(
            "/settings/weather",
            headers=admin.auth,
            data=WEATHER_FIXTURE_FORM,
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert admin.hub.hub_settings.weather.source == "fixture"

        after = client.get("/display/weather.png", headers=admin.auth)

    assert after.status_code == 200
    assert after.content != before.content


# ---------------------------------------------------------------------------
# save and test
# ---------------------------------------------------------------------------
def test_save_and_test_prints_the_adapter_error_for_a_bad_feed(admin: AdminHub) -> None:
    response = admin.client.post(
        "/settings/calendar",
        headers=admin.auth,
        data={
            "source": "ics",
            "feeds-0-url": "/no/such/calendar.ics",
            "feeds-0-name": "broken",
            "feeds-0-color": "blue",
            "agenda_days": "7",
            "ttl_seconds": "300",
            "action": "test",
        },
    )

    assert response.status_code == 200
    # The section was saved first, then fetched once: what the page shows is
    # the adapter's own Outcome (adapters/base.py), status and error string.
    assert admin.hub.settings_store.load("calendar").feeds[0].url == "/no/such/calendar.ics"
    body = response.text[response.text.index('id="calendar"') :]
    assert "Test:" in body
    assert "/no/such/calendar.ics" in body[: body.index("</section>")]


def test_save_and_test_reports_a_working_source(admin: AdminHub) -> None:
    response = admin.client.post(
        "/settings/weather", headers=admin.auth, data={**WEATHER_FIXTURE_FORM, "action": "test"}
    )

    assert response.status_code == 200
    body = response.text[response.text.index('id="weather"') :]
    assert "Test: ok" in body[: body.index("</section>")]


# ---------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------
def test_setup_done_sends_the_claimer_to_the_first_wizard_step(tmp_path: Path) -> None:
    app = create_app(env_for(tmp_path / "fresh"), HubSettings())
    client = TestClient(app, client=("127.0.0.1", 1))
    try:
        done = client.post(
            "/setup", data={"name": "deskmate", "base_url": "http://dashboard-hub.lan:8080"}
        )
        assert done.status_code == 200
        assert 'href="/setup/general"' in done.text
    finally:
        app.state.hub.db.close()


def test_each_step_renders_one_section_and_skips_to_the_next(admin: AdminHub) -> None:
    assert WIZARD_STEPS == ("general", "weather", "calendar", "home")

    for index, step in enumerate(WIZARD_STEPS):
        page = admin.client.get(f"/setup/{step}", headers=admin.auth)
        assert page.status_code == 200
        assert f'action="/setup/{step}"' in page.text
        for other in WIZARD_STEPS:
            if other != step:
                assert f'id="{other}"' not in page.text
        expected_skip = (
            f'href="/setup/{WIZARD_STEPS[index + 1]}"'
            if index + 1 < len(WIZARD_STEPS)
            else 'href="/settings"'
        )
        assert expected_skip in page.text


def test_a_wizard_step_saves_like_the_settings_page_and_moves_on(admin: AdminHub) -> None:
    response = admin.client.post(
        "/setup/general",
        headers=admin.auth,
        data={"timezone": "Europe/Berlin", "units": "metric", "action": "save"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/weather"
    assert admin.hub.settings_store.load("general").timezone == "Europe/Berlin"


def test_the_last_step_ends_at_the_settings_page(admin: AdminHub) -> None:
    response = admin.client.post(
        "/setup/home",
        headers=admin.auth,
        data={"source": "fixture", "url": "", "token": "", "ttl_seconds": "120", "action": "save"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_a_bad_value_keeps_the_wizard_on_its_step(admin: AdminHub) -> None:
    response = admin.client.post(
        "/setup/general",
        headers=admin.auth,
        data={"timezone": "Mars/Olympus", "units": "metric", "action": "save"},
    )

    assert response.status_code == 422
    assert 'action="/setup/general"' in response.text
    assert "unknown timezone" in response.text


def test_an_unknown_step_is_404(admin: AdminHub) -> None:
    assert admin.client.get("/setup/alert", headers=admin.auth).status_code == 404
    assert admin.client.post("/setup/alert", headers=admin.auth, data={}).status_code == 404


def test_an_unconfigured_hub_sends_the_wizard_back_to_setup(tmp_path: Path) -> None:
    app = create_app(env_for(tmp_path / "unclaimed"), HubSettings())
    client = TestClient(app)
    try:
        redirect = client.get("/setup/general", follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/setup"
    finally:
        app.state.hub.db.close()


def test_a_reader_is_refused_by_every_wizard_route(admin: AdminHub) -> None:
    reader = admin.reader_client()

    redirect = reader.get("/setup/general", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("/login")
    assert reader.post("/setup/general", data=GENERAL_FORM).status_code == 401
    # The device key as a bearer is a reader credential too, and just as
    # refused: it sits in the panel's unencrypted flash.
    assert admin.client.post(
        "/setup/general",
        headers={"Authorization": f"Bearer {admin.secrets.device_key}"},
        data=GENERAL_FORM,
    ).status_code == 401


# ---------------------------------------------------------------------------
# place search
# ---------------------------------------------------------------------------
PLACES_PAYLOAD = {
    "results": [
        {
            "name": "Bangkok",
            "admin1": "Bangkok",
            "country": "Thailand",
            "latitude": 13.75398,
            "longitude": 100.50144,
        },
        {
            "name": "Bang Kho Laem",
            "admin1": "Bangkok",
            "country": "Thailand",
            "latitude": 13.69306,
            "longitude": 100.5025,
        },
        {"name": "No coordinates", "country": "Nowhere"},
    ]
}


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def fake_client(recorder: list[tuple[str, dict[str, Any]]], payload: dict[str, Any] | None):
    """An ``httpx.AsyncClient`` stand-in: records the call, answers with
    ``payload``, or fails the way an unreachable upstream does."""

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
            recorder.append((url, dict(params or {})))
            if payload is None:
                raise httpx.ConnectError("upstream is down")
            return _FakeResponse(payload)

    return _FakeClient


def test_the_search_renders_its_results_as_radios_in_the_weather_form(
    admin: AdminHub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(geocode.httpx, "AsyncClient", fake_client(calls, PLACES_PAYLOAD))

    with caplog.at_level(logging.DEBUG):
        page = admin.client.get("/settings/geocode", params={"q": "  bangkok  "}, headers=admin.auth)

    assert page.status_code == 200
    assert calls == [
        (GEOCODING_URL, {"name": "bangkok", "count": RESULT_COUNT, "language": "en", "format": "json"})
    ]
    body = page.text[page.text.index('id="weather"') :]
    body = body[: body.index("</section>")]
    assert 'name="place" value="13.75398,100.50144,Bangkok"' in body
    assert "Bangkok, Bangkok, Thailand" in body
    # A result with no coordinates is dropped rather than rendered as a
    # radio that would store nothing.
    assert "No coordinates" not in body
    # The query is the owner's business: no line this hub writes carries it
    # (the test client's own httpx logger is not the hub's, and is why this
    # looks only at the app.* loggers).
    hub_lines = [record.getMessage().lower() for record in caplog.records if record.name.startswith("app")]
    assert not any("bangkok" in line for line in hub_lines)


def test_choosing_a_result_and_saving_stores_the_three_weather_fields(
    admin: AdminHub,
) -> None:
    response = admin.client.post(
        "/settings/weather",
        headers=admin.auth,
        data={
            "source": "open_meteo",
            "latitude": "",
            "longitude": "",
            "location_name": "",
            "ttl_seconds": "900",
            "place": "13.75398,100.50144,Bangkok, Thailand",
            "action": "save",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    stored = admin.hub.settings_store.load("weather")
    assert (stored.latitude, stored.longitude) == (13.75398, 100.50144)
    # Split at most twice, so a name with a comma in it survives.
    assert stored.location_name == "Bangkok, Thailand"


def test_an_upstream_failure_is_one_line_and_never_a_500(
    admin: AdminHub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(geocode.httpx, "AsyncClient", fake_client(calls, None))

    with caplog.at_level(logging.DEBUG):
        page = admin.client.get("/settings/geocode", params={"q": "bangkok"}, headers=admin.auth)

    assert page.status_code == 200
    assert page.text.count(SEARCH_FAILED) == 1
    # The failure is logged, but an httpx error prints the request URL and
    # the URL holds the query, so only the exception type is recorded.
    hub_lines = [record.getMessage() for record in caplog.records if record.name.startswith("app")]
    assert any("location search failed" in line for line in hub_lines)
    assert not any("bangkok" in line.lower() for line in hub_lines)


def test_the_query_is_capped_before_it_is_sent(
    admin: AdminHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(geocode.httpx, "AsyncClient", fake_client(calls, PLACES_PAYLOAD))

    admin.client.get("/settings/geocode", params={"q": "b" * 500}, headers=admin.auth)

    assert calls[0][1]["name"] == "b" * MAX_QUERY_LENGTH


def test_the_weather_wizard_step_searches_against_its_own_url(
    admin: AdminHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(geocode.httpx, "AsyncClient", fake_client(calls, PLACES_PAYLOAD))

    page = admin.client.get("/setup/weather", params={"q": "bangkok"}, headers=admin.auth)

    assert page.status_code == 200
    assert 'action="/setup/weather"' in page.text
    assert 'name="place" value="13.75398,100.50144,Bangkok"' in page.text


def test_the_search_is_admin_only(admin: AdminHub) -> None:
    reader = admin.reader_client()
    redirect = reader.get("/settings/geocode", params={"q": "bangkok"}, follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# the modules section
# ---------------------------------------------------------------------------
def test_the_modules_section_lists_every_installed_module(admin: AdminHub) -> None:
    """Including the ones with no row in the database yet: those show their
    manifest defaults, so installing a module is enough to see it here."""
    assert admin.hub.settings_store.load("modules") == ModulesSettings()
    page = admin.client.get("/settings", headers=admin.auth)

    assert 'id="modules"' in page.text
    for index, module in enumerate(admin.hub.registry.modules):
        assert f'value="{module.id}"' in page.text
        assert f'name="items-{index}-id"' in page.text
        assert f'name="items-{index}-enabled"' in page.text
        assert f'name="items-{index}-order"' in page.text


def test_saving_the_modules_section_disables_a_page_without_a_restart(
    admin: AdminHub,
) -> None:
    """The save reloads the hub, which rebuilds the registry, the renderer's
    view of it, the state service and the render cache."""
    assert "WEATHER" in window_names(admin)
    before = len(admin.hub.registry.page_ids())

    assert save_modules(admin, disable=frozenset({"weather"})).status_code == 303

    assert "weather" not in admin.hub.registry.page_ids()
    assert "weather" not in admin.hub.state_service.adapters
    assert "weather" not in admin.hub.renderer.pages
    # Gone from the footer, ...
    assert "WEATHER" not in window_names(admin)
    # ... from the id route, ...
    assert admin.client.get("/display/weather.png", headers=admin.auth).status_code == 404
    # ... and from the index route, which is one page shorter now.
    assert admin.client.get(f"/display/{before - 1}.png", headers=admin.auth).status_code == 404


def test_disabling_a_page_shrinks_the_telemetry_window_list(admin: AdminHub) -> None:
    """The device learns the list from this response and nowhere else, so a
    module disabled here has to be gone from it on the very next post."""
    device_auth = {"Authorization": f"Bearer {admin.secrets.device_key}"}
    payload = {"device": "reterminal-e1002", "page": "today"}

    before = admin.client.post(
        "/api/device/telemetry", json=payload, headers=device_auth
    ).json()
    assert "weather" in before["pages"]

    assert save_modules(admin, disable=frozenset({"weather"})).status_code == 303

    after = admin.client.post(
        "/api/device/telemetry", json=payload, headers=device_auth
    ).json()
    assert "weather" not in after["pages"]
    assert after["page_count"] == before["page_count"] - 1


def test_saving_the_modules_section_reorders_the_window_list(admin: AdminHub) -> None:
    admin.hub.settings_store.save(
        "modules", ModulesSettings(items=[ModuleToggle(id="system", order=1)])
    )
    asyncio.run(admin.hub.reload())
    assert admin.hub.registry.page_ids()[0] == "system"
    assert window_names(admin)[0] == "SYSTEM"


def test_disabling_a_dataset_module_leaves_its_pages_unavailable(admin: AdminHub) -> None:
    """``tasks`` draws no page of its own; the pages that read it must keep
    rendering, saying "unavailable" rather than inventing a task list."""
    assert save_modules(admin, disable=frozenset({"tasks"})).status_code == 303

    assert "tasks" not in admin.hub.registry.datasets()
    assert "tasks" not in admin.hub.state_service.adapters
    # today still draws, and DashboardState.block hands it a placeholder of
    # the right type instead of making the page None-check the dataset.
    state = state_at()
    assert state.block("tasks", TasksBlock).status is AdapterStatus.UNAVAILABLE
    html = admin.hub.renderer.render_html("today", state, embed_fonts=False)
    assert "tasks unavailable" in html


def test_an_id_that_is_not_installed_is_kept_and_warned_about(admin: AdminHub) -> None:
    assert save_modules(admin, extra_rows=("ghost",)).status_code == 303

    assert admin.hub.registry.missing_ids() == ("ghost",)
    page = admin.client.get("/settings", headers=admin.auth)
    assert "Not installed on this hub: ghost" in page.text
    # Kept, not dropped: the row is still in the database and still on the
    # form, so a module that comes back after an upgrade comes back enabled.
    stored = admin.hub.settings_store.load("modules")
    assert "ghost" in [row.id for row in stored.items]
    assert 'value="ghost"' in page.text


def test_disabling_every_page_is_refused_with_a_field_error(admin: AdminHub) -> None:
    """The panel has to have something to draw: a device asking for
    /display/0.png on a hub with no page would get a 404 forever."""
    pages = frozenset(admin.hub.registry.page_ids())
    response = save_modules(admin, disable=pages)

    assert response.status_code == 422
    assert "at least one module with a page has to stay enabled" in response.text
    # Nothing was written: the row is still the untouched one and every
    # page is still there.
    assert admin.hub.registry.page_ids()
    assert admin.hub.settings_store.load("modules") == ModulesSettings()


def test_today_may_be_disabled_while_another_page_stays(admin: AdminHub) -> None:
    """The rule is "no page left", not "never today": a hub whose owner
    wants only the agenda is allowed to say so."""
    assert save_modules(admin, disable=frozenset({"today"})).status_code == 303
    assert "today" not in admin.hub.registry.page_ids()
    assert admin.hub.registry.page_ids()
