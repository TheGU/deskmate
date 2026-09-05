"""Endpoint behaviour: health, state, ETag/304, cache busting, alerts, preview."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import etag_matches
from app.renderer.palette import DISPLAY_SIZE
from app.renderer.render import PAGES
from tests.conftest import open_png


def test_healthz_reports_every_adapter(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["timezone"] == "Asia/Bangkok"
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


def test_alert_round_trip_changes_the_alert_page(client: TestClient) -> None:
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

    cleared = client.delete("/api/alert")
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] is True
    assert client.get("/api/state").json()["alert"] is None


def test_alert_source_round_trips_and_reaches_the_page(client: TestClient) -> None:
    """The optional source names what raised the alert, in the sender's words."""
    client.delete("/api/alert")
    created = client.post(
        "/api/alert",
        json={
            "title": "Doorbell",
            "message": "Someone is at the door",
            "priority": "doorbell",
            "source": "Front door",
        },
    )
    assert created.status_code == 201
    assert created.json()["alert"]["source"] == "Front door"
    assert client.get("/api/state").json()["alert"]["source"] == "Front door"

    html = client.get("/preview/alert.html").text
    assert "FRONT DOOR" in html

    assert client.get("/display/alert.png").status_code == 200
    client.delete("/api/alert")


def test_alert_without_a_source_falls_back_to_the_priority(client: TestClient) -> None:
    client.delete("/api/alert")
    created = client.post(
        "/api/alert",
        json={"title": "Laundry done", "priority": "normal", "duration_seconds": 30},
    )
    assert created.status_code == 201
    assert created.json()["alert"]["source"] is None

    html = client.get("/preview/alert.html").text
    assert "NORMAL" in html
    client.delete("/api/alert")


def test_alert_rejects_a_lower_priority_while_one_is_active(client: TestClient) -> None:
    client.delete("/api/alert")
    high = client.post(
        "/api/alert",
        json={"title": "Smoke alarm", "priority": "critical", "duration_seconds": 120},
    )
    assert high.status_code == 201

    low = client.post(
        "/api/alert",
        json={"title": "Laundry done", "priority": "normal", "duration_seconds": 30},
    )
    assert low.status_code == 409
    assert low.json()["accepted"] is False
    assert low.json()["alert"]["title"] == "Smoke alarm"

    client.delete("/api/alert")


def test_alert_validation(client: TestClient) -> None:
    assert client.post("/api/alert", json={"title": ""}).status_code == 422
    assert (
        client.post("/api/alert", json={"title": "x", "priority": "nope"}).status_code == 422
    )
