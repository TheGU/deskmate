"""Endpoint behaviour: health, state, ETag/304, cache busting, alerts, preview.

The hub identity/auth flow (Part 1A) is inherently a one-shot state machine:
a fresh hub starts unconfigured, and claiming it is a single irreversible
transition (no edit or regenerate mode). ``hub_token`` below is an autouse,
module-scoped fixture: it claims the shared session ``client``'s hub once,
before any test in this module runs, regardless of which test pytest picks
first, so nothing here depends on test execution order. The full
unconfigured-to-claimed story (which needs a hub that is *not* claimed yet)
gets its own isolated app instead, in ``test_setup_flow_end_to_end``.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import REPO_ROOT, Settings
from app.main import MAX_OPEN_BODY_BYTES, create_app, etag_matches
from app.renderer.palette import DISPLAY_SIZE
from app.renderer.render import PAGES
from tests.conftest import open_png

FIXTURES_DIR = REPO_ROOT / "fixtures"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module", autouse=True)
def hub_token(client: TestClient) -> str:
    """Claim the shared session hub once for this module, before any test
    body runs. Bypasses the HTTP form (that flow is exercised on its own,
    isolated app in ``test_setup_flow_end_to_end``) so this has no ordering
    dependency on any other test.
    """
    hub = client.app.state.hub
    code = hub.identity.claim_code
    assert code is not None
    return asyncio.run(
        hub.identity.claim(
            submitted_code=code, name="deskmate", base_url="http://dashboard-hub.lan:8080"
        )
    )


def test_setup_flow_end_to_end(tmp_path: Path) -> None:
    """The whole unconfigured -> claimed story in one function, on its own
    isolated app: the shared session ``client``'s hub is always already
    claimed (see the ``hub_token`` autouse fixture above), so the
    unconfigured-state assertions below need a hub of their own.
    """
    data_dir = tmp_path
    settings = Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
    )
    app = create_app(settings)
    with TestClient(app) as flow_client:
        hub = flow_client.app.state.hub
        assert hub.identity.configured is False
        assert hub.identity.claim_code is not None

        redirect = flow_client.get("/", follow_redirects=False)
        assert redirect.status_code in (307, 308)
        assert redirect.headers["location"] == "/setup"

        unconfigured_page = flow_client.get("/setup")
        assert unconfigured_page.status_code == 200
        assert "claim code" in unconfigured_page.text.lower()

        assert flow_client.post("/api/alert", json={"title": "x"}).status_code == 503
        assert flow_client.delete("/api/alert").status_code == 503

        form = {"name": "deskmate", "base_url": "http://dashboard-hub.lan:8080"}
        wrong = flow_client.post("/setup", data={**form, "claim_code": "0000-0000"})
        assert wrong.status_code == 403
        assert hub.identity.configured is False

        code = hub.identity.claim_code
        assert code is not None
        right = flow_client.post("/setup", data={**form, "claim_code": code})
        assert right.status_code == 200
        assert right.headers["cache-control"] == "no-store"
        assert right.headers["pragma"] == "no-cache"
        match = re.search(r'id="token-value">([^<]+)</code>', right.text)
        assert match is not None, right.text
        token = match.group(1)
        assert len(token) > 20

        assert hub.identity.configured is True
        assert hub.identity.claim_code is None
        stored_text = hub.settings.hub_config_file.read_text(encoding="utf-8")
        stored = json.loads(stored_text)
        assert stored["token_sha256"] != token
        assert token not in stored_text

        configured_page = flow_client.get("/setup")
        assert configured_page.status_code == 200
        assert "already set up" in configured_page.text.lower()

        second = flow_client.post("/setup", data={**form, "claim_code": "AAAA-AAAA"})
        assert second.status_code == 409

        denied = flow_client.post("/api/alert", json={"title": "x"})
        assert denied.status_code == 401
        allowed = flow_client.post(
            "/api/alert", json={"title": "x"}, headers=auth(token)
        )
        assert allowed.status_code == 201
        flow_client.delete("/api/alert", headers=auth(token))


def test_post_setup_rejects_a_form_over_the_cap(tmp_path: Path) -> None:
    """A form with Content-Length over MAX_OPEN_BODY_BYTES is rejected before
    request.form() ever reads it."""
    settings = Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    app = create_app(settings)
    with TestClient(app) as oversized_client:
        oversized = {"name": "x" * (MAX_OPEN_BODY_BYTES + 1)}
        response = oversized_client.post("/setup", data=oversized)
        assert response.status_code == 413


def test_a_corrupt_hub_config_503s_setup_and_writes_but_the_panel_keeps_working(
    tmp_path: Path,
) -> None:
    (tmp_path / "hub.json").write_text("{ not json", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    app = create_app(settings)
    with TestClient(app) as broken_client:
        hub = broken_client.app.state.hub
        assert hub.identity.error is not None
        assert hub.identity.claim_code is None

        setup = broken_client.get("/setup")
        assert setup.status_code == 503
        assert "hub config unreadable" in setup.json()["detail"]

        alert = broken_client.post("/api/alert", json={"title": "x"})
        assert alert.status_code == 503
        assert "hub config unreadable" in alert.json()["detail"]

        # The panel routes do not depend on hub identity at all.
        assert broken_client.get("/healthz").status_code == 200
        assert broken_client.get("/api/state").status_code == 200
        assert broken_client.get("/display/today.png").status_code == 200


def test_api_hub_reports_identity_and_sources(client: TestClient, hub_token: str) -> None:
    payload = client.get("/api/hub").json()
    assert payload["configured"] is True
    assert payload["name"] == "deskmate"
    assert payload["base_url"] == "http://dashboard-hub.lan:8080"
    # The test DATA_DIR is empty, so "auto" (the default) resolves to fixture.
    for dataset in ("ai_usage", "brief", "tasks"):
        assert payload["sources"][dataset]["configured"] == "auto"
        assert payload["sources"][dataset]["effective"] == "fixture"


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
    settings = Settings(
        _env_file=None,
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=blocked,
        LOG_LEVEL="WARNING",
    )
    with pytest.raises(RuntimeError, match="not writable"):
        create_app(settings)


def test_healthz_reports_every_adapter(client: TestClient) -> None:
    # /healthz only replays each adapter's last outcome; force one first.
    client.get("/api/state")
    response = client.get("/healthz")
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


def test_healthz_before_any_state_build_is_unknown_and_disconnected(tmp_path: Path) -> None:
    """No ``with`` lifespan: Chromium never launches and no adapter has ever
    fetched, so /healthz must still answer 200 without triggering either."""
    settings = Settings(
        _env_file=None,
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    app = create_app(settings)
    fresh_client = TestClient(app)
    response = fresh_client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["renderer"]["connected"] is False
    for name, block in payload["adapters"].items():
        assert block["status"] == "unknown", f"{name} is {block['status']}"
        assert block["updated_at"] is None
        assert block["error"] is None


def test_api_state_returns_normalized_state(client: TestClient) -> None:
    payload = client.get("/api/state").json()
    assert payload["timezone"] == "Asia/Bangkok"
    assert payload["tasks"]["status"] == "ok"
    assert payload["tasks"]["items"], "fixture tasks should not be empty"
    assert payload["weather"]["weather"]["location_name"] == "Bangkok"


@pytest.mark.parametrize("page", PAGES)
def test_display_returns_a_png_of_the_right_size(client: TestClient, page: str) -> None:
    response = client.get(f"/display/{page}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"].startswith('"')
    assert open_png(response.content).size == DISPLAY_SIZE


def test_display_rejects_an_unknown_page(client: TestClient) -> None:
    assert client.get("/display/nope.png").status_code == 404


def test_if_none_match_gives_304(client: TestClient) -> None:
    first = client.get("/display/today.png")
    etag = first.headers["etag"]
    second = client.get("/display/today.png", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["etag"] == etag
    assert second.content == b""


def test_if_none_match_tolerates_weak_and_list_forms(client: TestClient) -> None:
    etag = client.get("/display/today.png").headers["etag"]
    weak = client.get("/display/today.png", headers={"If-None-Match": f"W/{etag}"})
    assert weak.status_code == 304
    listed = client.get(
        "/display/today.png", headers={"If-None-Match": f'"deadbeef", {etag}'}
    )
    assert listed.status_code == 304


def test_stale_etag_returns_the_image(client: TestClient) -> None:
    response = client.get("/display/today.png", headers={"If-None-Match": '"stale"'})
    assert response.status_code == 200
    assert response.content


def test_cache_bust_query_still_serves_the_image(client: TestClient) -> None:
    response = client.get("/display/today.png", params={"t": "12345"})
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


def test_preview_lists_every_page(client: TestClient) -> None:
    response = client.get("/preview")
    assert response.status_code == 200
    for page in PAGES:
        assert f"/preview?page={page}" in response.text


@pytest.mark.parametrize("page", PAGES)
def test_preview_html_renders(client: TestClient, page: str) -> None:
    response = client.get(f"/preview/{page}.html")
    assert response.status_code == 200
    assert "<html" in response.text
    assert "Google Sans" in response.text


def test_preview_html_rejects_an_unknown_page(client: TestClient) -> None:
    assert client.get("/preview/nope.html").status_code == 404


def test_preview_rgb_returns_a_png_of_the_right_size(client: TestClient) -> None:
    response = client.get("/preview/today-rgb.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert open_png(response.content).size == DISPLAY_SIZE


def test_preview_rgb_rejects_an_unknown_page(client: TestClient) -> None:
    assert client.get("/preview/nope-rgb.png").status_code == 404


def test_root_redirects_to_preview(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/preview"


def test_alert_round_trip_changes_the_alert_page(client: TestClient, hub_token: str) -> None:
    before = client.get("/display/alert.png")
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

    after = client.get("/display/alert.png")
    assert after.status_code == 200
    assert after.content != before.content
    assert after.headers["etag"] != before.headers["etag"]

    state = client.get("/api/state").json()
    assert state["alert"]["title"] == "Doorbell"
    assert state["alert"]["priority"] == "doorbell"

    cleared = client.delete("/api/alert", headers=auth(hub_token))
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] is True
    assert client.get("/api/state").json()["alert"] is None


def test_alert_source_round_trips_and_reaches_the_page(
    client: TestClient, hub_token: str
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
    assert client.get("/api/state").json()["alert"]["source"] == "Front door"

    html = client.get("/preview/alert.html").text
    assert "FRONT DOOR" in html

    assert client.get("/display/alert.png").status_code == 200
    client.delete("/api/alert", headers=auth(hub_token))


def test_alert_without_a_source_falls_back_to_the_priority(
    client: TestClient, hub_token: str
) -> None:
    client.delete("/api/alert", headers=auth(hub_token))
    created = client.post(
        "/api/alert",
        json={"title": "Laundry done", "priority": "normal", "duration_seconds": 30},
        headers=auth(hub_token),
    )
    assert created.status_code == 201
    assert created.json()["alert"]["source"] is None

    html = client.get("/preview/alert.html").text
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
