"""Endpoint behaviour: health, state, ETag/304, cache busting, alerts, preview.

The hub identity/auth flow (Part 1A) is inherently a one-shot state machine:
a fresh hub starts unconfigured, and claiming it is a single irreversible
transition (no edit or regenerate mode). ``hub_claim`` below is an autouse,
module-scoped fixture: it claims the shared session ``client``'s hub once,
before any test in this module runs, regardless of which test pytest picks
first, so nothing here depends on test execution order. The full
unconfigured-to-claimed story (which needs a hub that is *not* claimed yet)
gets its own isolated app instead, in ``test_setup_flow_end_to_end``.

Reads are now gated once the hub is claimed (require_reader / require_device
/ require_reader_html in app/hub_config.py), so most GET calls in this file
go through ``reader`` - a thin wrapper (see ``_ReaderClient``) that replays
every call through the shared session ``client`` with a bearer token added,
rather than the bare ``client``. It deliberately is not a second TestClient:
that would open a second anyio portal thread, and the renderer's Chromium is
a Playwright browser bound to the first portal's event loop, so a
render-touching call routed through a different portal hangs forever.
``client`` stays unauthenticated for the auth-negative tests and the setup
flow, and keeps its explicit ``auth(token)`` headers for the write routes
(POST/DELETE /api/alert), which require_token still gates unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Env
from app.db import Database
from app.hub_config import ADMIN_SESSION_MAX_AGE_SECONDS, COOKIE_NAME, ClaimedSecrets, session_role
from app.httputil import MAX_OPEN_BODY_BYTES
from app.main import create_app, etag_matches
from app.renderer.palette import DISPLAY_SIZE
from app.renderer.render import PAGES
from tests.conftest import open_png


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module", autouse=True)
def hub_claim(client: TestClient) -> ClaimedSecrets:
    """Claim the shared session hub once for this module, before any test
    body runs. Bypasses the HTTP form (that flow is exercised on its own,
    isolated app in ``test_setup_flow_end_to_end``) so this has no ordering
    dependency on any other test.
    """
    hub = client.app.state.hub
    return asyncio.run(
        hub.identity.claim(name="deskmate", base_url="http://dashboard-hub.lan:8080")
    )


@pytest.fixture(scope="module")
def hub_token(hub_claim: ClaimedSecrets) -> str:
    return hub_claim.token


@pytest.fixture(scope="module")
def device_key(hub_claim: ClaimedSecrets) -> str:
    return hub_claim.device_key


class _ReaderClient:
    """A thin wrapper around the shared session ``client``, injecting a
    bearer token on every ``get()`` rather than being a second TestClient.

    A second TestClient over the same app opens its own anyio portal thread
    (its own event loop); the renderer's Chromium is a Playwright browser
    bound to the *first* TestClient's portal loop (see conftest.py), so any
    render-touching call routed through a different portal hangs forever.
    Routing every call back through the one ``client`` object keeps
    everything on that one portal, while still letting each test read with
    a credential without threading auth() through every call site.
    """

    def __init__(self, client: TestClient, token: str) -> None:
        self._client = client
        self._token = token

    @property
    def client(self) -> TestClient:
        """The wrapped client, for a test that needs ``app.state.hub``."""
        return self._client

    def get(self, url: str, **kwargs: Any) -> Any:
        headers = {**auth(self._token), **kwargs.pop("headers", {})}
        return self._client.get(url, headers=headers, **kwargs)


@pytest.fixture(scope="module")
def reader(client: TestClient, hub_token: str) -> _ReaderClient:
    return _ReaderClient(client, hub_token)


def test_setup_flow_end_to_end(tmp_path: Path) -> None:
    """The whole unconfigured -> claimed story in one function, on its own
    isolated app: the shared session ``client``'s hub is always already
    claimed (see the ``hub_claim`` autouse fixture above), so the
    unconfigured-state assertions below need a hub of their own.
    """
    data_dir = tmp_path
    env = Env(
        _env_file=None,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
    )
    app = create_app(env)
    # An explicit private client address: is_private_client_host now fails
    # closed on TestClient's default, unparseable "testclient" host, and
    # this flow's two POST /setup calls (claim, then the already-configured
    # 409) must reach the claim check to exercise what they are about.
    with TestClient(app, client=("127.0.0.1", 1)) as flow_client:
        hub = flow_client.app.state.hub
        assert hub.identity.configured is False

        redirect = flow_client.get("/", follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/setup"

        unconfigured_page = flow_client.get("/setup")
        assert unconfigured_page.status_code == 200
        assert "claim code" not in unconfigured_page.text.lower()

        assert flow_client.post("/api/alert", json={"title": "x"}).status_code == 503
        assert flow_client.delete("/api/alert").status_code == 503

        # A name distinct from the generic "deskmate" page title, so the
        # no-leak assertion below cannot pass by accident.
        form = {"name": "My Test Hub", "base_url": "http://dashboard-hub.lan:8080"}
        right = flow_client.post("/setup", data=form)
        assert right.status_code == 200
        assert right.headers["cache-control"] == "no-store"
        assert right.headers["pragma"] == "no-cache"
        match = re.search(r'id="token-value">([^<]+)</code>', right.text)
        assert match is not None, right.text
        token = match.group(1)
        assert len(token) > 20

        assert hub.identity.configured is True
        with hub.db.reading() as connection:
            stored = dict(connection.execute("SELECT * FROM hub WHERE id = 1").fetchone())
        # Only the hash is stored; the plaintext token is not recoverable
        # from any column of the row.
        assert stored["token_sha256"] != token
        assert token not in [str(value) for value in stored.values()]

        # The claimer was just shown the token on this page: setup-done
        # signs them in as admin right there, cookie included.
        admin_cookie = right.cookies.get(COOKIE_NAME)
        assert admin_cookie
        assert session_role(hub.identity.config.session_secret, admin_cookie, time.time()) == "admin"
        assert f"Max-Age={ADMIN_SESSION_MAX_AGE_SECONDS}" in right.headers["set-cookie"]

        configured_page = flow_client.get("/setup")
        assert configured_page.status_code == 200
        assert "already set up" in configured_page.text.lower()
        # An anonymous caller must not learn the name, base URL or creation
        # time of a configured hub from GET /setup.
        assert form["name"] not in configured_page.text
        assert form["base_url"] not in configured_page.text

        second = flow_client.post("/setup", data=form)
        assert second.status_code == 409

        denied = flow_client.post("/api/alert", json={"title": "x"})
        assert denied.status_code == 401
        allowed = flow_client.post(
            "/api/alert", json={"title": "x"}, headers=auth(token)
        )
        assert allowed.status_code == 201
        flow_client.delete("/api/alert", headers=auth(token))


def test_post_setup_rejects_a_non_private_client(tmp_path: Path) -> None:
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    app = create_app(env)
    # 8.8.8.8 (unlike the RFC 5737 documentation ranges, which Python's
    # ipaddress module - surprisingly - classifies as "private") is squarely
    # public.
    public_client = TestClient(app, client=("8.8.8.8", 12345))
    response = public_client.post(
        "/setup", data={"name": "deskmate", "base_url": "http://dashboard-hub.lan:8080"}
    )
    assert response.status_code == 403
    assert "local network" in response.json()["detail"]
    assert public_client.app.state.hub.identity.configured is False


def test_get_setup_prefill_uses_the_first_forwarded_proto(tmp_path: Path) -> None:
    """The setup page's guessed base URL and _telemetry_origin's hub_host
    share one helper for X-Forwarded-Proto (app.main._forwarded_scheme): a
    proxy chain lists the original client's scheme first in a comma
    separated value, and the guess must take that first one, not the whole
    raw header."""
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    unconfigured_client = TestClient(create_app(env))
    response = unconfigured_client.get(
        "/setup", headers={"X-Forwarded-Proto": "https, http", "Host": "dashboard-hub.lan"}
    )
    assert response.status_code == 200
    assert 'value="https://dashboard-hub.lan"' in response.text


def test_reads_and_device_telemetry_503_while_unconfigured(tmp_path: Path) -> None:
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    unconfigured_client = TestClient(create_app(env))
    assert unconfigured_client.get("/api/state").status_code == 503
    assert unconfigured_client.get("/display/today.png").status_code == 503
    assert (
        unconfigured_client.post(
            "/api/device/telemetry", json={"device": "reterminal-e1002"}
        ).status_code
        == 503
    )


def test_preview_and_root_redirect_to_setup_while_unconfigured(tmp_path: Path) -> None:
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    unconfigured_client = TestClient(create_app(env))
    preview_redirect = unconfigured_client.get("/preview", follow_redirects=False)
    assert preview_redirect.status_code == 303
    assert preview_redirect.headers["location"] == "/setup"

    root_redirect = unconfigured_client.get("/", follow_redirects=False)
    assert root_redirect.status_code == 303
    assert root_redirect.headers["location"] == "/setup"


def test_login_redirects_to_setup_with_a_get_not_a_repost_while_unconfigured(
    tmp_path: Path,
) -> None:
    """A plain (default) 307 redirect re-sends the original method and body,
    which would turn a POST /login into a POST /setup carrying the login
    form's fields. Both /login handlers must answer 303 instead, so a
    browser's next request to /setup is a GET."""
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    unconfigured_client = TestClient(create_app(env))
    get_redirect = unconfigured_client.get("/login", follow_redirects=False)
    assert get_redirect.status_code == 303
    assert get_redirect.headers["location"] == "/setup"

    post_redirect = unconfigured_client.post(
        "/login", data={"key": "whatever", "next": "/preview"}, follow_redirects=False
    )
    assert post_redirect.status_code == 303
    assert post_redirect.headers["location"] == "/setup"


def test_healthz_minimal_body_while_unconfigured(tmp_path: Path) -> None:
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    unconfigured_client = TestClient(create_app(env))
    response = unconfigured_client.get("/healthz")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "version", "renderer"}


def test_post_setup_rejects_a_form_over_the_cap(tmp_path: Path) -> None:
    """A form with Content-Length over MAX_OPEN_BODY_BYTES is rejected before
    request.form() ever reads it."""
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    app = create_app(env)
    # A private client address so the size cap is what rejects this request,
    # not the (now fail-closed) is_private_client_host check on TestClient's
    # default "testclient" host.
    with TestClient(app, client=("127.0.0.1", 1)) as oversized_client:
        oversized = {"name": "x" * (MAX_OPEN_BODY_BYTES + 1)}
        response = oversized_client.post("/setup", data=oversized)
        assert response.status_code == 413


def test_a_corrupt_hub_config_503s_setup_and_writes_but_the_panel_keeps_working(
    tmp_path: Path,
) -> None:
    # A hub row that is present but cannot be trusted: created_at is not a
    # timestamp. Written before create_app, into the same database file the
    # app will open from the registry.
    seed = Database(tmp_path / "deskmate.sqlite")
    seed.migrate()
    with seed.writing() as connection:
        connection.execute(
            "INSERT INTO hub (id, name, base_url, token_sha256, device_key_sha256,"
            " session_secret, created_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
            ("deskmate", "http://dashboard-hub.lan:8080", "x", "y", "s", "not-a-timestamp"),
        )
    seed.close()
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    app = create_app(env)
    with TestClient(app) as broken_client:
        hub = broken_client.app.state.hub
        assert hub.identity.error is not None

        setup = broken_client.get("/setup")
        assert setup.status_code == 503
        assert "hub config unreadable" in setup.json()["detail"]

        alert = broken_client.post("/api/alert", json={"title": "x"})
        assert alert.status_code == 503
        assert "hub config unreadable" in alert.json()["detail"]

        # /healthz never consults identity.error, so it alone stays 200. The
        # read routes now go through require_reader, which - like every
        # other identity-backed dependency - 503s on a config it cannot
        # trust; they are no longer identity-independent.
        assert broken_client.get("/healthz").status_code == 200
        assert broken_client.get("/api/state").status_code == 503
        assert broken_client.get("/display/today.png").status_code == 503


def test_api_hub_reports_identity_and_sources(reader: _ReaderClient) -> None:
    payload = reader.get("/api/hub").json()
    assert payload["configured"] is True
    assert payload["name"] == "deskmate"
    assert payload["base_url"] == "http://dashboard-hub.lan:8080"
    # The session hub_settings fixture pins every source to fixture (see conftest.py).
    for dataset in ("ai_usage", "brief", "tasks"):
        assert payload["sources"][dataset]["source"] == "fixture"


def test_api_hub_requires_a_reader_credential(client: TestClient) -> None:
    assert client.get("/api/hub").status_code == 401


def test_alert_requires_the_token(client: TestClient, hub_token: str) -> None:
    assert client.post("/api/alert", json={"title": "x"}).status_code == 401
    assert client.post(
        "/api/alert", json={"title": "x"}, headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    response = client.post("/api/alert", json={"title": "x"}, headers=auth(hub_token))
    assert response.status_code == 201
    assert client.delete("/api/alert", headers=auth(hub_token)).status_code == 200


def test_data_dir_that_is_a_file_fails_fast_at_startup(tmp_path: Path) -> None:
    """A wrong owner on the bind mount, or DATA_DIR pointed at a plain file,
    must fail at startup with one clear line instead of at the first push."""
    blocked = tmp_path / "data"
    blocked.write_text("not a directory", encoding="utf-8")
    env = Env(
        _env_file=None,
        DATA_DIR=blocked,
        LOG_LEVEL="WARNING",
    )
    with pytest.raises(RuntimeError, match="not writable"):
        create_app(env)


def test_healthz_reports_every_adapter(reader: _ReaderClient) -> None:
    # /healthz only replays each adapter's last outcome; force one first.
    reader.get("/api/state")
    response = reader.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["timezone"] == "Asia/Bangkok"
    assert payload["renderer"]["connected"] is True
    assert set(payload["adapters"]) == {
        "tasks",
        "calendar",
        "weather",
        "ai_usage",
        "brief",
        "home",
        "device",
    }
    for name, block in payload["adapters"].items():
        assert block["status"] == "ok", f"{name} is {block['status']}: {block['error']}"


def test_healthz_on_a_claimed_hub_hides_adapters_until_authenticated(
    client: TestClient, reader: _ReaderClient
) -> None:
    unauthenticated = client.get("/healthz")
    assert unauthenticated.status_code == 200
    assert "adapters" not in unauthenticated.json()

    authenticated = reader.get("/healthz")
    assert authenticated.status_code == 200
    assert "adapters" in authenticated.json()


def test_healthz_before_any_state_build_is_unknown_and_disconnected(tmp_path: Path) -> None:
    """No ``with`` lifespan: Chromium never launches and no adapter has ever
    fetched, so /healthz must still answer 200 without triggering either.
    Claimed (but with no reader auth on this bare TestClient) so the full
    body - and the adapter statuses this test is about - is visible.
    """
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    app = create_app(env)
    fresh_client = TestClient(app)
    secrets = asyncio.run(
        fresh_client.app.state.hub.identity.claim(
            name="deskmate", base_url="http://dashboard-hub.lan:8080"
        )
    )
    response = fresh_client.get("/healthz", headers=auth(secrets.token))
    assert response.status_code == 200
    payload = response.json()
    assert payload["renderer"]["connected"] is False
    for name, block in payload["adapters"].items():
        assert block["status"] == "unknown", f"{name} is {block['status']}"
        assert block["updated_at"] is None
        assert block["error"] is None


def test_api_state_returns_normalized_state(reader: _ReaderClient) -> None:
    """Schema 2: every dataset under ``blocks``, keyed by its name.

    The task title and the location name are the ``SerializeAsAny`` guard
    (models.py:DashboardState): ``blocks`` is declared as the base ``Block``,
    so without it pydantic would serialize seven bare envelopes and this
    body would carry no data at all.
    """
    payload = reader.get("/api/state").json()
    assert payload["schema"] == 2
    assert payload["timezone"] == "Asia/Bangkok"
    blocks = payload["blocks"]
    assert blocks["tasks"]["status"] == "ok"
    assert blocks["tasks"]["items"], "fixture tasks should not be empty"
    assert blocks["tasks"]["items"][0]["title"]
    assert blocks["weather"]["weather"]["location_name"] == "Bangkok"


def test_api_state_requires_a_reader_credential(client: TestClient) -> None:
    assert client.get("/api/state").status_code == 401


@pytest.mark.parametrize("page", PAGES)
def test_display_returns_a_png_of_the_right_size(reader: _ReaderClient, page: str) -> None:
    response = reader.get(f"/display/{page}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"].startswith('"')
    assert open_png(response.content).size == DISPLAY_SIZE


def test_display_rejects_an_unknown_page(reader: _ReaderClient) -> None:
    assert reader.get("/display/nope.png").status_code == 404


def test_display_by_index_serves_the_nth_enabled_page(reader: _ReaderClient) -> None:
    """The device walks the pages by number; 0 is the first enabled page."""
    registry = reader.client.app.state.hub.registry
    for index, page_id in enumerate(registry.page_ids()):
        response = reader.get(f"/display/{index}.png")
        assert response.status_code == 200
        # The header is the id, never the index: the cache key is the id, and
        # the telemetry the device posts back names the page it is showing.
        assert response.headers["x-deskmate-page"] == page_id


def test_display_by_index_shares_the_cache_entry_with_the_id(reader: _ReaderClient) -> None:
    by_id = reader.get("/display/today.png")
    by_index = reader.get("/display/0.png")
    assert by_index.headers["etag"] == by_id.headers["etag"]
    assert by_index.content == by_id.content


def test_display_by_index_past_the_last_page_is_404(reader: _ReaderClient) -> None:
    count = len(reader.client.app.state.hub.registry.page_ids())
    assert reader.get(f"/display/{count}.png").status_code == 404
    assert reader.get("/display/999.png").status_code == 404


def test_alert_is_never_an_index(reader: _ReaderClient) -> None:
    """``alert`` interrupts and hands the page back; it takes no slot."""
    registry = reader.client.app.state.hub.registry
    assert "alert" not in registry.page_ids()
    assert reader.get("/display/alert.png").headers["x-deskmate-page"] == "alert"


def test_display_requires_a_reader_credential(client: TestClient) -> None:
    assert client.get("/display/today.png").status_code == 401


def test_display_accepts_the_device_key(client: TestClient, device_key: str) -> None:
    response = client.get("/display/today.png", headers=auth(device_key))
    assert response.status_code == 200


def test_display_accepts_the_token(client: TestClient, hub_token: str) -> None:
    response = client.get("/display/today.png", headers=auth(hub_token))
    assert response.status_code == 200


def test_if_none_match_gives_304(reader: _ReaderClient) -> None:
    first = reader.get("/display/today.png")
    etag = first.headers["etag"]
    second = reader.get("/display/today.png", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["etag"] == etag
    assert second.content == b""


def test_if_none_match_tolerates_weak_and_list_forms(reader: _ReaderClient) -> None:
    etag = reader.get("/display/today.png").headers["etag"]
    weak = reader.get("/display/today.png", headers={"If-None-Match": f"W/{etag}"})
    assert weak.status_code == 304
    listed = reader.get(
        "/display/today.png", headers={"If-None-Match": f'"deadbeef", {etag}'}
    )
    assert listed.status_code == 304


def test_stale_etag_returns_the_image(reader: _ReaderClient) -> None:
    response = reader.get("/display/today.png", headers={"If-None-Match": '"stale"'})
    assert response.status_code == 200
    assert response.content


def test_cache_bust_query_still_serves_the_image(reader: _ReaderClient) -> None:
    response = reader.get("/display/today.png", params={"t": "12345"})
    assert response.status_code == 200
    assert open_png(response.content).size == DISPLAY_SIZE


def test_etag_matches_helper() -> None:
    assert etag_matches('"abc"', '"abc"')
    assert etag_matches('W/"abc"', '"abc"')
    assert etag_matches('"x", "abc"', '"abc"')
    assert etag_matches("*", '"abc"')
    assert not etag_matches('"other"', '"abc"')
    assert not etag_matches(None, '"abc"')
    assert not etag_matches("", '"abc"')


def test_preview_lists_every_page(reader: _ReaderClient) -> None:
    response = reader.get("/preview")
    assert response.status_code == 200
    for page in PAGES:
        assert f"/preview?page={page}" in response.text


def test_preview_has_a_settings_link(reader: _ReaderClient) -> None:
    response = reader.get("/preview")
    assert 'href="/settings"' in response.text


def test_preview_does_not_eagerly_load_the_panel_png(reader: _ReaderClient) -> None:
    """The hidden panel <img> must not have a src on page load: fetching the
    Playwright render on every navigation makes even the raw view wait on
    the render lock. It carries the URL in data-src instead, and a small
    script assigns src only once the panel view is actually shown."""
    response = reader.get("/preview")
    assert ' src="/display' not in response.text
    assert 'data-src="/display' in response.text


def test_preview_without_a_credential_redirects_to_login(client: TestClient) -> None:
    response = client.get("/preview", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/preview"


@pytest.mark.parametrize("page", PAGES)
def test_preview_html_renders(reader: _ReaderClient, page: str) -> None:
    response = reader.get(f"/preview/{page}.html")
    assert response.status_code == 200
    assert "<html" in response.text
    assert "Google Sans" in response.text


def test_preview_html_rejects_an_unknown_page(reader: _ReaderClient) -> None:
    assert reader.get("/preview/nope.html").status_code == 404




def test_root_redirects_to_preview(reader: _ReaderClient) -> None:
    response = reader.get("/", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/preview"


def test_login_with_the_wrong_key_is_rejected(client: TestClient) -> None:
    response = client.post("/login", data={"key": "not-a-real-key", "next": "/preview"})
    assert response.status_code == 401


def test_login_rejects_an_oversized_body(client: TestClient) -> None:
    oversized = {"key": "x" * (MAX_OPEN_BODY_BYTES + 1)}
    assert client.post("/login", data=oversized).status_code == 413


def test_login_with_the_device_key_sets_a_cookie_good_for_preview_and_state(
    client: TestClient, device_key: str
) -> None:
    response = client.post(
        "/login", data={"key": device_key, "next": "/preview"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/preview"
    cookie = response.cookies.get(COOKIE_NAME)
    assert cookie

    cookie_only = TestClient(client.app, cookies={COOKIE_NAME: cookie})
    assert cookie_only.get("/preview").status_code == 200
    assert cookie_only.get("/api/state").status_code == 200

    # The cookie is a reader credential only: never accepted on a write
    # route, nor on the device-telemetry route (a browser tab signed in to
    # /preview must not be able to inject a reading or fire an alert).
    assert cookie_only.post("/api/alert", json={"title": "x"}).status_code == 401
    assert cookie_only.post("/api/device/telemetry", json={"device": "x"}).status_code == 401


# -- roles: /settings and /settings/general ---------------------------------
# These are about who reaches the two routes, not about what they store, so
# they post an empty form: 422 (the general section's timezone is missing)
# is the answer for a caller the guard let through, and 401 or a redirect to
# /login for one it did not. Saving is tested in test_settings_page.py, which
# builds its own hub: a real save here would reload the shared session hub
# out from under every other test in the suite.
def test_device_key_bearer_is_refused_at_the_settings_routes(
    client: TestClient, device_key: str
) -> None:
    """The device key is a valid reader credential but never an admin one
    (require_admin/require_admin_html check the bearer with verify_token
    only): POST is a plain 401, GET is sent to /login rather than a bare
    401 page."""
    assert (
        client.post("/settings/general", data={}, headers=auth(device_key)).status_code == 401
    )
    redirect = client.get("/settings", headers=auth(device_key), follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("/login")


def test_token_bearer_is_accepted_at_the_settings_routes(
    client: TestClient, hub_token: str
) -> None:
    assert (
        client.post("/settings/general", data={}, headers=auth(hub_token)).status_code == 422
    )
    page = client.get("/settings", headers=auth(hub_token))
    assert page.status_code == 200
    assert "Settings" in page.text


def test_login_with_the_token_yields_an_admin_cookie_that_reaches_settings(
    client: TestClient, hub_token: str
) -> None:
    response = client.post(
        "/login", data={"key": hub_token, "next": "/preview"}, follow_redirects=False
    )
    assert response.status_code == 303
    cookie = response.cookies.get(COOKIE_NAME)
    assert cookie
    config = client.app.state.hub.identity.config
    assert session_role(config.session_secret, cookie, time.time()) == "admin"
    assert f"Max-Age={ADMIN_SESSION_MAX_AGE_SECONDS}" in response.headers["set-cookie"]

    cookie_only = TestClient(client.app, cookies={COOKIE_NAME: cookie})
    assert cookie_only.get("/settings").status_code == 200
    assert cookie_only.post("/settings/general", data={}).status_code == 422


def test_login_with_the_device_key_yields_a_reader_cookie_turned_away_from_settings(
    client: TestClient, device_key: str
) -> None:
    response = client.post(
        "/login", data={"key": device_key, "next": "/preview"}, follow_redirects=False
    )
    assert response.status_code == 303
    cookie = response.cookies.get(COOKIE_NAME)
    assert cookie
    config = client.app.state.hub.identity.config
    assert session_role(config.session_secret, cookie, time.time()) == "reader"

    cookie_only = TestClient(client.app, cookies={COOKIE_NAME: cookie})
    assert cookie_only.get("/preview").status_code == 200
    redirect = cookie_only.get("/settings", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("/login")
    assert cookie_only.post("/settings/general", data={}).status_code == 401


def test_a_reader_cookie_is_refused_by_admin_authenticated(
    client: TestClient, device_key: str
) -> None:
    """A cookie minted for the reader role (here, by signing in with the
    device key) must never pass admin_authenticated, on either settings
    route."""
    login = client.post(
        "/login", data={"key": device_key, "next": "/preview"}, follow_redirects=False
    )
    reader_cookie = login.cookies.get(COOKIE_NAME)
    assert reader_cookie
    cookie_only = TestClient(client.app, cookies={COOKIE_NAME: reader_cookie})
    assert cookie_only.post("/settings/general", data={}).status_code == 401
    redirect = cookie_only.get("/settings", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("/login")


def test_an_old_format_cookie_is_rejected_everywhere(client: TestClient) -> None:
    """A cookie in the pre-role "<exp>.<mac>" format (the MAC over
    "browser|<exp>") must be rejected by every guard, reader and admin
    alike - never treated as a valid, let alone admin, session."""
    config = client.app.state.hub.identity.config
    exp = int(time.time() + 1000)
    old_mac = hmac.new(
        config.session_secret.encode("utf-8"), f"browser|{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    old_cookie = f"{exp}.{old_mac}"
    cookie_only = TestClient(client.app, cookies={COOKIE_NAME: old_cookie})
    assert cookie_only.get("/preview", follow_redirects=False).status_code == 303
    assert cookie_only.get("/api/state").status_code == 401
    redirect = cookie_only.get("/settings", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("/login")
    assert cookie_only.post("/settings/general", data={}).status_code == 401


def test_post_settings_general_rejects_an_oversized_body(
    client: TestClient, hub_token: str
) -> None:
    oversized = {"anything": "x" * (MAX_OPEN_BODY_BYTES + 1)}
    response = client.post("/settings/general", data=oversized, headers=auth(hub_token))
    assert response.status_code == 413


def test_device_telemetry_post_accepts_the_device_key_bearer_on_a_claimed_hub(
    tmp_path: Path,
) -> None:
    """Its own isolated app and DATA_DIR, not the shared session ``client``:
    a real insert here would land in the same deskmate.sqlite the session
    ``state``/``renderer`` fixtures read, and perturb device-chart
    assertions in other test files that rebuild state fresh from it.
    """
    env = Env(
        _env_file=None,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    app = create_app(env)
    with TestClient(app) as isolated_client:
        hub = isolated_client.app.state.hub
        secrets = asyncio.run(
            hub.identity.claim(name="deskmate", base_url="http://dashboard-hub.lan:8080")
        )
        response = isolated_client.post(
            "/api/device/telemetry",
            json={"device": "reterminal-e1002", "temperature": 30.0},
            headers=auth(secrets.device_key),
        )
        assert response.status_code == 202


def test_alert_round_trip_changes_the_alert_page(
    client: TestClient, reader: _ReaderClient, hub_token: str
) -> None:
    before = reader.get("/display/alert.png")
    assert before.status_code == 200

    created = client.post(
        "/api/alert",
        json={
            "title": "Doorbell",
            "message": "Someone is at the door",
            "priority": "doorbell",
            "duration_seconds": 90,
        },
        headers=auth(hub_token),
    )
    assert created.status_code == 201
    assert created.json()["accepted"] is True

    after = reader.get("/display/alert.png")
    assert after.status_code == 200
    assert after.content != before.content
    assert after.headers["etag"] != before.headers["etag"]

    state = reader.get("/api/state").json()
    assert state["alert"]["title"] == "Doorbell"
    assert state["alert"]["priority"] == "doorbell"

    cleared = client.delete("/api/alert", headers=auth(hub_token))
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] is True
    assert reader.get("/api/state").json()["alert"] is None


def test_alert_source_round_trips_and_reaches_the_page(
    client: TestClient, reader: _ReaderClient, hub_token: str
) -> None:
    """The optional source names what raised the alert, in the sender's words."""
    client.delete("/api/alert", headers=auth(hub_token))
    created = client.post(
        "/api/alert",
        json={
            "title": "Doorbell",
            "message": "Someone is at the door",
            "priority": "doorbell",
            "source": "Front door",
        },
        headers=auth(hub_token),
    )
    assert created.status_code == 201
    assert created.json()["alert"]["source"] == "Front door"
    assert reader.get("/api/state").json()["alert"]["source"] == "Front door"

    html = reader.get("/preview/alert.html").text
    assert "FRONT DOOR" in html

    assert reader.get("/display/alert.png").status_code == 200
    client.delete("/api/alert", headers=auth(hub_token))


def test_alert_without_a_source_falls_back_to_the_priority(
    client: TestClient, reader: _ReaderClient, hub_token: str
) -> None:
    client.delete("/api/alert", headers=auth(hub_token))
    created = client.post(
        "/api/alert",
        json={"title": "Laundry done", "priority": "normal", "duration_seconds": 30},
        headers=auth(hub_token),
    )
    assert created.status_code == 201
    assert created.json()["alert"]["source"] is None

    html = reader.get("/preview/alert.html").text
    assert "NORMAL" in html
    client.delete("/api/alert", headers=auth(hub_token))


def test_alert_rejects_a_lower_priority_while_one_is_active(
    client: TestClient, hub_token: str
) -> None:
    client.delete("/api/alert", headers=auth(hub_token))
    high = client.post(
        "/api/alert",
        json={"title": "Smoke alarm", "priority": "critical", "duration_seconds": 120},
        headers=auth(hub_token),
    )
    assert high.status_code == 201

    low = client.post(
        "/api/alert",
        json={"title": "Laundry done", "priority": "normal", "duration_seconds": 30},
        headers=auth(hub_token),
    )
    assert low.status_code == 409
    assert low.json()["accepted"] is False
    assert low.json()["alert"]["title"] == "Smoke alarm"

    client.delete("/api/alert", headers=auth(hub_token))


def test_alert_validation(client: TestClient, hub_token: str) -> None:
    assert (
        client.post("/api/alert", json={"title": ""}, headers=auth(hub_token)).status_code
        == 422
    )
    assert (
        client.post(
            "/api/alert", json={"title": "x", "priority": "nope"}, headers=auth(hub_token)
        ).status_code
        == 422
    )
