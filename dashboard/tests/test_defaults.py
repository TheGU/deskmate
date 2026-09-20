"""The honest-empty-state guarantee: with every settings section left at its
default (the live selectors ``test_settings.py`` already asserts), every
adapter must still resolve to something (never fixture data), and every page
must still render a context - unavailable everywhere, never an exception -
against an empty ``DATA_DIR``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.alerts import AlertStore
from app.config import Env
from app.db import get_database
from app.renderer.render import PAGES
from app.settings import HubSettings
from app.state import StateService
from app.view import build_context


@pytest.fixture()
def env_with_no_sources_configured(tmp_path: Path) -> Env:
    return Env(_env_file=None, DATA_DIR=tmp_path)


def test_defaults_against_an_empty_data_dir_render_every_page_all_unavailable(
    env_with_no_sources_configured: Env,
) -> None:
    """No fixtures, no pushed files, no ICS URLs, no HA URL, no telemetry
    ever posted: every block must come back unavailable (never fixture data,
    never an exception), and every one of the six pages must still build a
    context from that state."""
    env = env_with_no_sources_configured
    hub_settings = HubSettings()
    database = get_database(env.hub_db_file)
    database.migrate()
    alerts = AlertStore(database, hub_settings.general.timezone)
    service = StateService(hub_settings, env, alerts)
    state = asyncio.run(service.build(force=True))

    for name, block in state.blocks.items():
        assert not block.usable, f"{name} should be unavailable with no sources configured"

    for page in PAGES:
        context = build_context(page, state, hub_settings)
        assert context["page"] == page
