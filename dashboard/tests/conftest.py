"""Shared fixtures.

Chromium is expensive to start, so the session keeps one event loop and one
:class:`Renderer` on it. Playwright objects are bound to the loop that created
them, so every renderer call in the tests goes through :func:`run`.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.alerts import AlertStore
from app.config import REPO_ROOT, Settings
from app.db import close_databases, get_database
from app.main import create_app
from app.models import DashboardState
from app.renderer.render import Renderer
from app.settings import HubSettings
from app.state import StateService

FIXTURES_DIR = REPO_ROOT / "fixtures"

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` on the session event loop."""
    assert _loop is not None, "session loop fixture was not requested"
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="session", autouse=True)
def session_loop() -> Iterator[asyncio.AbstractEventLoop]:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    yield _loop
    _loop.close()
    _loop = None


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    data_dir: Path = tmp_path_factory.mktemp("data")
    # _env_file=None keeps a developer's .env out of the test run.
    return Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
        # Every *_SOURCE now defaults to a live selector so a real deployment
        # never shows demo data by accident; the shared renderer/state
        # fixtures below pin all seven back to fixture so tests keep
        # exercising fixture data as before.
        TASKS_SOURCE="fixture",
        CALENDAR_SOURCE="fixture",
        WEATHER_SOURCE="fixture",
        AI_USAGE_SOURCE="fixture",
        BRIEF_SOURCE="fixture",
        HA_SOURCE="fixture",
        DEVICE_SOURCE="fixture",
    )


@pytest.fixture(scope="session")
def hub_settings(settings: Settings) -> HubSettings:
    """A ``HubSettings`` built from the shared ``settings`` fixture, the same
    way 1.2b will build one at startup. Unused by any test until 1.2b
    switches call sites; it exists now so those tests do not also have to
    add this fixture."""
    return HubSettings.from_env(settings)


@pytest.fixture(scope="session")
def renderer(settings: Settings, session_loop: asyncio.AbstractEventLoop) -> Iterator[Renderer]:
    instance = Renderer(settings)
    run(instance.start())
    yield instance
    run(instance.close())


@pytest.fixture(scope="session", autouse=True)
def close_open_databases() -> Iterator[None]:
    """Every database in the process-wide registry is closed once, at the end
    of the session: they are keyed by path and shared on purpose, so no single
    test may close one."""
    yield
    close_databases()


@pytest.fixture(scope="session")
def state(settings: Settings, session_loop: asyncio.AbstractEventLoop) -> DashboardState:
    database = get_database(settings.hub_db_file)
    database.migrate()
    alerts = AlertStore(database, settings.timezone)
    service = StateService(settings, alerts)
    return run(service.build(force=True))


@pytest.fixture(scope="session")
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def open_png(payload: bytes) -> Image.Image:
    """Decode PNG bytes into a loaded Pillow image."""
    image = Image.open(io.BytesIO(payload))
    image.load()
    return image
