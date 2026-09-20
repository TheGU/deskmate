"""Shared fixtures.

Chromium is expensive to start, so the session keeps one event loop and one
:class:`Renderer` on it. Playwright objects are bound to the loop that created
them, so every renderer call in the tests goes through :func:`run`.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Coroutine, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.alerts import AlertStore
from app.config import Env
from app.db import close_databases, get_database
from app.main import create_app
from app.models import Alert, Block, DashboardState
from app.modules.ai_usage.settings import AIUsageSettings
from app.modules.brief.settings import BriefSettings
from app.modules.calendar.settings import CalendarSettings
from app.modules.device.settings import DeviceSettings
from app.modules.home.settings import HomeSettings
from app.modules.tasks.settings import TasksSettings
from app.modules.weather.settings import WeatherSettings
from app.renderer.render import Renderer
from app.settings import HubSettings
from app.state import StateService

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
def env(tmp_path_factory: pytest.TempPathFactory) -> Env:
    data_dir: Path = tmp_path_factory.mktemp("data")
    # _env_file=None keeps a developer's .env out of the test run.
    return Env(
        _env_file=None,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
    )


@pytest.fixture(scope="session")
def hub_settings() -> HubSettings:
    """Every section built directly from its own model, not from ``Settings``
    (config.py) through ``from_env``: this is the settings-page/database
    world 1.2b's call sites read, and the point of this fixture is that
    tests stop depending on ``config.Settings`` wherever they can. Every
    source pinned to ``fixture`` so the shared ``renderer``/``state``/
    ``client`` fixtures below keep exercising fixture data, the same as the
    old ``settings`` fixture's seven ``*_SOURCE=fixture`` overrides did.
    """
    return HubSettings(
        tasks=TasksSettings(source="fixture"),
        calendar=CalendarSettings(source="fixture"),
        weather=WeatherSettings(source="fixture"),
        ai_usage=AIUsageSettings(source="fixture"),
        brief=BriefSettings(source="fixture"),
        home=HomeSettings(source="fixture"),
        device=DeviceSettings(source="fixture"),
    )


@pytest.fixture(scope="session")
def renderer(
    env: Env, hub_settings: HubSettings, session_loop: asyncio.AbstractEventLoop
) -> Iterator[Renderer]:
    instance = Renderer(env, hub_settings)
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
def state(
    env: Env, hub_settings: HubSettings, session_loop: asyncio.AbstractEventLoop
) -> DashboardState:
    database = get_database(env.hub_db_file)
    database.migrate()
    alerts = AlertStore(database, hub_settings.general.timezone)
    service = StateService(hub_settings, env, alerts)
    return run(service.build(force=True))


@pytest.fixture(scope="session")
def client(env: Env, hub_settings: HubSettings) -> Iterator[TestClient]:
    app = create_app(env, hub_settings)
    with TestClient(app) as test_client:
        yield test_client


def make_state(
    *,
    generated_at: datetime,
    timezone: str = "Asia/Bangkok",
    alert: Alert | None = None,
    **blocks: Block,
) -> DashboardState:
    """A :class:`DashboardState` from block keyword arguments.

    ``DashboardState`` keys its blocks by dataset name since 2.1a (a module
    brings its own datasets, so there is no fixed set of fields any more),
    but a test reads far better as ``make_state(..., tasks=TasksBlock(...))``
    than as a hand-built mapping. This is that one line of sugar, and
    nothing else: every name lands in ``state.blocks`` untouched.
    """
    return DashboardState(
        generated_at=generated_at, timezone=timezone, blocks=dict(blocks), alert=alert
    )


def with_blocks(state: DashboardState, **blocks: Block) -> DashboardState:
    """``state`` with these blocks replaced and every other one kept."""
    return state.model_copy(update={"blocks": {**state.blocks, **blocks}})


def open_png(payload: bytes) -> Image.Image:
    """Decode PNG bytes into a loaded Pillow image."""
    image = Image.open(io.BytesIO(payload))
    image.load()
    return image
