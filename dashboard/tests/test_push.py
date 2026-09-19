"""The three push endpoints: validation, the ``datasets`` row each one
writes, and the adapters/state they feed.

Uses its own module-scoped app (separate from tests/test_http.py's session
client) so the sequential claim story here does not interleave with that
file's. The hub is claimed once for the module; datasets are pushed in a
fixed order.

1.2d note: ``push`` is now the only "real" tasks/ai_usage/brief selector
(``obsidian`` and ``fixture`` are the other two); it reads the matching row
of the ``datasets`` table (``app/datasets.py``) directly, so unlike the old
``auto`` selector it never falls back to fixture data on its own before the
first push - a fresh hub on ``push`` shows ``unavailable`` until something is
posted. The "auto resolves to fixture before, file after" story this file
used to test belonged to that now-unreachable selector; the tests below
that depended on it are gone or rewritten (see the render-gate note in
docs/plan/2026-09-19-settings-modules-provisioning.md).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import REPO_ROOT, Env
from app.datasets import read_dataset
from app.db import get_database
from app.main import create_app
from app.modules.ai_usage.settings import AIUsageSettings
from app.modules.brief.settings import BriefSettings
from app.modules.tasks.settings import TasksSettings
from app.settings import HubSettings

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
    env = Env(
        _env_file=None,
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
    )
    # tasks/ai_usage/brief default to "push" already (test_settings.py's
    # defaults test); spelled out here so the fixture reads as this
    # module's own story rather than relying on a default staying put.
    hub_settings = HubSettings(
        tasks=TasksSettings(source="push"),
        ai_usage=AIUsageSettings(source="push"),
        brief=BriefSettings(source="push"),
    )
    app = create_app(env, hub_settings)
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def token(push_client: TestClient) -> str:
    return _claim(push_client)


def test_hub_sources_report_the_configured_selector(push_client: TestClient, token: str) -> None:
    sources = push_client.get("/api/hub", headers=auth(token)).json()["sources"]
    for dataset in ("ai_usage", "brief", "tasks"):
        assert sources[dataset]["source"] == "push"


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
    # Same UTC offset as the hub's own Asia/Bangkok timezone, so re-localizing
    # for display must leave the wall-clock value unchanged, not shifted.
    assert provider["collected_at"].startswith("2026-09-16T10:00:00")


def test_post_ai_usage_valid_writes_the_dataset_row_and_reaches_state(
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
    assert body["stored"] == "ai_usage"
    assert body["count"] == 1
    assert body["effective_source"] == "push"
    assert "warning" not in body

    database = get_database(data_dir / "deskmate.sqlite")
    found = read_dataset(database, "ai_usage")
    assert found is not None
    payload, received_at = found
    assert payload["providers"][0]["provider"] == "claude"
    assert received_at is not None

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


def test_post_brief_valid_writes_the_dataset_row_and_reaches_state(
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
    assert body["stored"] == "brief"
    assert body["count"] == 1
    assert body["effective_source"] == "push"

    database = get_database(data_dir / "deskmate.sqlite")
    found = read_dataset(database, "brief")
    assert found is not None
    payload, received_at = found
    assert payload["headline"] == "Two deadlines today"
    assert received_at is not None

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


def test_post_tasks_valid_writes_the_dataset_row_and_reaches_state(
    push_client: TestClient, token: str, data_dir: Path
) -> None:
    response = push_client.post(
        "/api/tasks",
        json={"tasks": [{"id": "agent-1", "title": "Ship the release notes", "priority": "high"}]},
        headers=auth(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stored"] == "tasks"
    assert body["count"] == 1
    assert body["effective_source"] == "push"

    database = get_database(data_dir / "deskmate.sqlite")
    found = read_dataset(database, "tasks")
    assert found is not None
    payload, received_at = found
    assert payload["tasks"][0]["id"] == "agent-1"
    assert received_at is not None

    state = push_client.get("/api/state", headers=auth(token)).json()
    items = state["tasks"]["items"]
    assert [item["id"] for item in items] == ["agent-1"]
    assert state["tasks"]["received_at"] is not None


# -- warning when the selector is explicitly "fixture" -----------------------
def test_push_warns_when_the_selector_is_fixture(tmp_path_factory: pytest.TempPathFactory) -> None:
    data_dir = tmp_path_factory.mktemp("push-fixture-only")
    env = Env(
        _env_file=None,
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
    )
    hub_settings = HubSettings(
        ai_usage=AIUsageSettings(source="fixture"),
        brief=BriefSettings(source="fixture"),
        tasks=TasksSettings(source="fixture"),
    )
    app = create_app(env, hub_settings)
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

    assert ai_usage.json()["warning"] == "ai_usage.source is fixture; the panel will not show this push"
    assert brief.json()["warning"] == "brief.source is fixture; the panel will not show this push"
    assert tasks.json()["warning"] == "tasks.source is fixture; the panel will not show this push"
    assert ai_usage.json()["effective_source"] == "fixture"
    assert brief.json()["effective_source"] == "fixture"
    assert tasks.json()["effective_source"] == "fixture"


def test_push_tasks_warns_and_reports_obsidian_when_that_is_the_selector(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Pushing tasks.json while the tasks source is obsidian does not change
    what the panel shows (it still reads the vault): effective_source names
    the selector verbatim ("obsidian", not "fixture"), and the warning does
    too."""
    data_dir = tmp_path_factory.mktemp("push-obsidian")
    env = Env(
        _env_file=None,
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
    )
    hub_settings = HubSettings(tasks=TasksSettings(source="obsidian"))
    app = create_app(env, hub_settings)
    with TestClient(app) as client:
        obsidian_token = _claim(client)
        response = client.post("/api/tasks", json={"tasks": []}, headers=auth(obsidian_token))
    body = response.json()
    assert body["effective_source"] == "obsidian"
    assert body["warning"] == "tasks.source is obsidian; the panel will not show this push"
