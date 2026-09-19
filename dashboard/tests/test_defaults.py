"""The seven ``*_SOURCE`` defaults, and the honest-empty-state guarantee that
depends on them: with no environment configured at all, every adapter must
still resolve to something (its live default, not fixture), and every page
must still render a context - unavailable everywhere, never an exception -
against an empty ``DATA_DIR``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.alerts import AlertStore
from app.config import REPO_ROOT, Settings
from app.db import get_database
from app.renderer.render import PAGES
from app.state import StateService
from app.view import build_context

FIXTURES_DIR = REPO_ROOT / "fixtures"

#: Every *_SOURCE env var a developer's shell or .env might otherwise set;
#: cleared so this test proves the *defaults*, not whatever the environment
#: happens to carry.
SOURCE_ENV_VARS = (
    "TASKS_SOURCE",
    "CALENDAR_SOURCE",
    "WEATHER_SOURCE",
    "AI_USAGE_SOURCE",
    "BRIEF_SOURCE",
    "HA_SOURCE",
    "DEVICE_SOURCE",
)


@pytest.fixture()
def settings_with_no_sources_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    for name in SOURCE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # _env_file=None: a developer's .env must not backfill what the
    # environment was just cleared of.
    return Settings(_env_file=None, FIXTURES_DIR=FIXTURES_DIR, DATA_DIR=tmp_path)


def test_the_seven_source_defaults_are_the_live_selectors(
    settings_with_no_sources_configured: Settings,
) -> None:
    settings = settings_with_no_sources_configured
    assert settings.tasks_source == "file"
    assert settings.calendar_source == "ics"
    assert settings.weather_source == "open_meteo"
    assert settings.ai_usage_source == "file"
    assert settings.brief_source == "file"
    assert settings.ha_source == "rest"
    assert settings.device_source == "store"


def test_defaults_against_an_empty_data_dir_render_every_page_all_unavailable(
    settings_with_no_sources_configured: Settings,
) -> None:
    """No fixtures, no pushed files, no ICS URLs, no HA URL, no telemetry
    ever posted: every block must come back unavailable (never fixture data,
    never an exception), and every one of the six pages must still build a
    context from that state."""
    settings = settings_with_no_sources_configured
    database = get_database(settings.hub_db_file)
    database.migrate()
    alerts = AlertStore(database, settings.timezone)
    service = StateService(settings, alerts)
    state = asyncio.run(service.build(force=True))

    for name, block in state.blocks.items():
        assert not block.usable, f"{name} should be unavailable with no sources configured"

    for page in PAGES:
        context = build_context(page, state, settings)
        assert context["page"] == page
