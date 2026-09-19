"""The three push endpoints (Part 1B): validation, atomic writes, and the
adapters/state they feed.

Uses its own module-scoped app (separate from tests/test_http.py's session
client) so the sequential claim story here does not interleave with that
file's. The hub is claimed once for the module; datasets are pushed in a
fixed order so the "auto resolves to fixture before, file after" assertions
each see the state they expect.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import REPO_ROOT, Settings
from app.main import _write_json_atomic, create_app

FIXTURES_DIR = REPO_ROOT / "fixtures"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _claim(client: TestClient) -> str:
    hub = client.app.state.hub
    secrets = asyncio.run(
        hub.identity.claim(name="deskmate", base_url="http://dashboard-hub.lan:8080")
    )
    return secrets.token


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("push-data")


@pytest.fixture(scope="module")
def push_client(data_dir: Path) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
        # This module tests the "auto" selector's own resolution (fixture
        # before a push, file after), which is no longer the default; pin it
        # explicitly so that story still holds regardless of the default.
        AI_USAGE_SOURCE="auto",
        BRIEF_SOURCE="auto",
        TASKS_SOURCE="auto",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def token(push_client: TestClient) -> str:
    return _claim(push_client)


def test_write_json_atomic_leaves_no_tmp_when_the_payload_cannot_serialize(
    tmp_path: Path,
) -> None:
    class Unserializable:
        pass

    target = tmp_path / "out.json"
    with pytest.raises(TypeError):
        _write_json_atomic(target, {"bad": Unserializable()})
    assert list(tmp_path.glob("*.tmp")) == []
    assert not target.exists()


def test_auto_reports_fixture_before_any_push(push_client: TestClient, token: str) -> None:
    sources = push_client.get("/api/hub", headers=auth(token)).json()["sources"]
    for dataset in ("ai_usage", "brief", "tasks"):
        assert sources[dataset]["configured"] == "auto"
        assert sources[dataset]["effective"] == "fixture"


# -- ai-usage ----------------------------------------------------------------
def test_post_ai_usage_rejects_an_unknown_field(push_client: TestClient, token: str) -> None:
    response = push_client.post(
        "/api/ai-usage",
        json={"providers": [{"provider": "claude"}], "bogus": 1},
        headers=auth(token),
    )
    assert response.status_code == 422


def test_post_ai_usage_rejects_schema_version_2(push_client: TestClient, token: str) -> None:
    response = push_client.post(
        "/api/ai-usage",
        json={"schema_version": 2, "providers": [{"provider": "claude"}]},
        headers=auth(token),
    )
    assert response.status_code == 422


def test_post_ai_usage_rejects_more_than_eight_providers(
    push_client: TestClient, token: str
) -> None:
    providers = [{"provider": f"p{i}"} for i in range(9)]
    response = push_client.post(
        "/api/ai-usage", json={"providers": providers}, headers=auth(token)
    )
    assert response.status_code == 422


def test_post_ai_usage_requires_the_token(push_client: TestClient) -> None:
    response = push_client.post("/api/ai-usage", json={"providers": [{"provider": "claude"}]})
    assert response.status_code == 401


def test_post_ai_usage_rejects_a_naive_collected_at(push_client: TestClient, token: str) -> None:
    response = push_client.post(
        "/api/ai-usage",
        json={"providers": [{"provider": "claude", "collected_at": "2026-09-16T10:00:00"}]},
        headers=auth(token),
    )
    assert response.status_code == 422


def test_post_ai_usage_preserves_an_aware_collected_at(
    push_client: TestClient, token: str
) -> None:
    response = push_client.post(
        "/api/ai-usage",
        json={
            "providers": [{"provider": "aware-check", "collected_at": "2026-09-16T10:00:00+07:00"}]
        },
        headers=auth(token),
    )
    assert response.status_code == 200
    state = push_client.get("/api/state", headers=auth(token)).json()
    provider = next(p for p in state["ai_usage"]["providers"] if p["provider"] == "aware-check")
    # Same UTC offset as the hub's own Asia/Bangkok TIMEZONE, so re-localizing
    # for display must leave the wall-clock value unchanged, not shifted.
    assert provider["collected_at"].startswith("2026-09-16T10:00:00")


def test_post_ai_usage_valid_writes_atomically_and_reaches_state(
    push_client: TestClient, token: str, data_dir: Path
) -> None:
    response = push_client.post(
        "/api/ai-usage",
        json={
            "providers": [
                {
                    "provider": "claude",
                    "short_window_percent_remaining": 62,
                    "weekly_percent_remaining": 40,
                }
            ]
        },
        headers=auth(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stored"] == "ai-usage.json"
    assert body["count"] == 1
    # AI_USAGE_SOURCE is "auto" and this push just wrote the file, so the
    # panel will serve "file" now, not the raw "auto" selector.
    assert body["effective_source"] == "file"
    assert "warning" not in body

    assert list(data_dir.glob("*.tmp")) == []
    assert (data_dir / "ai-usage.json").is_file()

    state = push_client.get("/api/state", headers=auth(token)).json()
    providers = state["ai_usage"]["providers"]
    assert providers[0]["provider"] == "claude"
    assert providers[0]["short_window_percent_remaining"] == 62
    assert state["ai_usage"]["received_at"] is not None


# -- brief --------------------------------------------------------------------
def test_post_brief_rejects_a_naive_generated_at(push_client: TestClient, token: str) -> None:
    response = push_client.post(
        "/api/brief",
        json={"headline": "x", "generated_at": "2026-09-16T10:00:00"},
        headers=auth(token),
    )
    assert response.status_code == 422


def test_post_brief_rejects_an_unknown_field(push_client: TestClient, token: str) -> None:
    response = push_client.post(
        "/api/brief", json={"headline": "x", "extra": True}, headers=auth(token)
    )
    assert response.status_code == 422


def test_post_brief_rejects_schema_version_2(push_client: TestClient, token: str) -> None:
    response = push_client.post(
        "/api/brief",
        json={"schema_version": 2, "headline": "x"},
        headers=auth(token),
    )
    assert response.status_code == 422


def test_post_brief_rejects_more_than_six_sections(push_client: TestClient, token: str) -> None:
    sections = [{"title": f"s{i}", "items": []} for i in range(7)]
    response = push_client.post(
        "/api/brief",
        json={"headline": "x", "sections": sections},
        headers=auth(token),
    )
    assert response.status_code == 422


def test_post_brief_valid_writes_atomically_and_reaches_state(
    push_client: TestClient, token: str, data_dir: Path
) -> None:
    response = push_client.post(
        "/api/brief",
        json={
            "headline": "Two deadlines today",
            "note": "Answer the vendor quote before standup.",
            "sections": [{"title": "Key tasks", "items": ["Ship it"]}],
        },
        headers=auth(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stored"] == "current.json"
    assert body["count"] == 1
    assert body["effective_source"] == "file"

    brief_dir = data_dir / "brief"
    assert list(brief_dir.glob("*.tmp")) == []
    assert (brief_dir / "current.json").is_file()

    state = push_client.get("/api/state", headers=auth(token)).json()
    assert state["brief"]["brief"]["headline"] == "Two deadlines today"
    assert state["brief"]["received_at"] is not None


# -- tasks ----------------------------------------------------------------
def test_post_tasks_rejects_duplicate_ids(push_client: TestClient, token: str) -> None:
    response = push_client.post(
        "/api/tasks",
        json={"tasks": [{"id": "a", "title": "one"}, {"id": "a", "title": "two"}]},
        headers=auth(token),
    )
    assert response.status_code == 422


def test_post_tasks_rejects_more_than_sixty(push_client: TestClient, token: str) -> None:
    tasks = [{"id": f"t{i}", "title": f"task {i}"} for i in range(61)]
    response = push_client.post("/api/tasks", json={"tasks": tasks}, headers=auth(token))
    assert response.status_code == 422


def test_post_tasks_valid_writes_atomically_and_reaches_state(
    push_client: TestClient, token: str, data_dir: Path
) -> None:
    response = push_client.post(
        "/api/tasks",
        json={"tasks": [{"id": "agent-1", "title": "Ship the release notes", "priority": "high"}]},
        headers=auth(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stored"] == "tasks.json"
    assert body["count"] == 1
    assert body["effective_source"] == "file"

    assert list(data_dir.glob("*.tmp")) == []
    assert (data_dir / "tasks.json").is_file()

    state = push_client.get("/api/state", headers=auth(token)).json()
    items = state["tasks"]["items"]
    assert [item["id"] for item in items] == ["agent-1"]
    assert state["tasks"]["received_at"] is not None


# -- after all three pushes ----------------------------------------------
def test_auto_reports_file_after_the_pushes(push_client: TestClient, token: str) -> None:
    sources = push_client.get("/api/hub", headers=auth(token)).json()["sources"]
    for dataset in ("ai_usage", "brief", "tasks"):
        assert sources[dataset]["configured"] == "auto"
        assert sources[dataset]["effective"] == "file"


# -- warning when the selector is explicitly "fixture" -----------------------
def test_push_warns_when_the_selector_is_fixture(tmp_path_factory: pytest.TempPathFactory) -> None:
    data_dir = tmp_path_factory.mktemp("push-fixture-only")
    settings = Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
        AI_USAGE_SOURCE="fixture",
        BRIEF_SOURCE="fixture",
        TASKS_SOURCE="fixture",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        fixture_token = _claim(client)
        ai_usage = client.post(
            "/api/ai-usage",
            json={"providers": [{"provider": "claude"}]},
            headers=auth(fixture_token),
        )
        brief = client.post(
            "/api/brief", json={"headline": "x"}, headers=auth(fixture_token)
        )
        tasks = client.post("/api/tasks", json={"tasks": []}, headers=auth(fixture_token))

    assert ai_usage.json()["warning"] == "AI_USAGE_SOURCE is fixture; the panel will not show this push"
    assert brief.json()["warning"] == "BRIEF_SOURCE is fixture; the panel will not show this push"
    assert tasks.json()["warning"] == "TASKS_SOURCE is fixture; the panel will not show this push"
    assert ai_usage.json()["effective_source"] == "fixture"
    assert brief.json()["effective_source"] == "fixture"
    assert tasks.json()["effective_source"] == "fixture"


def test_push_tasks_warns_and_reports_obsidian_when_that_is_the_selector(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Pushing tasks.json while TASKS_SOURCE=obsidian does not change what
    the panel shows (it still reads the vault): effective_source names the
    selector verbatim ("obsidian", not "fixture"), and the warning does too."""
    data_dir = tmp_path_factory.mktemp("push-obsidian")
    settings = Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
        TASKS_SOURCE="obsidian",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        obsidian_token = _claim(client)
        response = client.post("/api/tasks", json={"tasks": []}, headers=auth(obsidian_token))
    body = response.json()
    assert body["effective_source"] == "obsidian"
    assert body["warning"] == "TASKS_SOURCE is obsidian; the panel will not show this push"


def test_hub_info_effective_reflects_a_push_immediately_before_any_fetch(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """GET /api/hub's "effective" is a live check, not the last fetch's
    delegate: right after a push, before any /api/state or page render has
    re-fetched tasks, it must already say "file"."""
    data_dir = tmp_path_factory.mktemp("push-immediate")
    settings = Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
        # TASKS_SOURCE is no longer "auto" by default; pin it here so this
        # "auto resolves live" story still holds.
        TASKS_SOURCE="auto",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        immediate_token = _claim(client)
        before = client.get("/api/hub", headers=auth(immediate_token)).json()["sources"]["tasks"][
            "effective"
        ]
        pushed = client.post("/api/tasks", json={"tasks": []}, headers=auth(immediate_token))
        assert pushed.status_code == 200
        after = client.get("/api/hub", headers=auth(immediate_token)).json()["sources"]["tasks"][
            "effective"
        ]
    assert before == "fixture"
    assert after == "file"
