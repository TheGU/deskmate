"""The module API and the registry: what loads, what refuses, what shows up.

The interesting case is a module nobody shipped: a package dropped into
``DATA_DIR/modules/``, with its own settings-free page, its own template
directory and its own dataset. If that reaches the window list, the render
and ``/display/<id>.png`` without core knowing its name, the contract in
docs/plan/2026-09-19-settings-modules-provisioning.md (phase 2) holds.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import BaseModel
from starlette.datastructures import FormData

from app.config import Env
from app.db import get_database
from app.forms import parse_section, render_section
from app.main import Hub, create_app
from app.models import TasksBlock
from app.modules import (
    DatasetSpec,
    Module,
    ModuleError,
    PageSpec,
    validate_module,
)
from app.modules.registry import (
    ModulesSettings,
    ModuleToggle,
    Registry,
    builtin_modules,
    builtin_registry,
    directory_modules,
)
from app.renderer.palette import DISPLAY_SIZE
from app.renderer.render import Renderer
from app.settings import SECTIONS, HubSettings
from app.settings_pages import TESTABLE_SECTIONS
from tests.conftest import make_state, run
from tests.test_forms import submission
from tests.test_settings_page import AdminHub

#: A whole module in one file, written into ``DATA_DIR/modules/hello/``. It
#: brings a page with its own template directory and one dataset whose
#: adapter needs nothing at all, so what the test proves is the wiring, not
#: the module's own cleverness.
HELLO_PACKAGE = '''\
"""A module that exists only in a test's temp directory."""

from __future__ import annotations

from pathlib import Path

from app.models import Task, TasksBlock
from app.modules import DatasetSpec, HeaderSpec, Module, PageSpec

HERE = Path(__file__).resolve().parent


class GreetingAdapter:
    name = "greeting"
    source = "static"

    async def fetch(self) -> list[Task]:
        return [Task(id="hello-1", title="Say hello")]


def greeting_context(state, settings):
    return {"greeting": "HELLO FROM A THIRD PARTY"}


def greeting_header(state, settings):
    return {"line": "HELLO FROM THE HEADER"}


MODULE = Module(
    id="hello",
    title="HELLO",
    version="1.0.0",
    description="A page and a dataset that core has never heard of.",
    datasets=(
        DatasetSpec(
            name="greeting",
            block_model=TasksBlock,
            value_field="items",
            section="tasks",
            build_adapter=lambda section, general, context: GreetingAdapter(),
            ttl_seconds=lambda section: 60.0,
        ),
    ),
    page=PageSpec(
        title="HELLO",
        templates_dir=HERE / "templates",
        template="hello.html",
        context=greeting_context,
        render_ttl_seconds=60.0,
        needs=("greeting",),
        flag=lambda state, settings: False,
    ),
    header=HeaderSpec(context=greeting_header, templates_dir=HERE / "templates"),
    default_order=90,
)
'''

HELLO_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <style>
      {{ font_css }}
      html, body { margin: 0; width: 800px; height: 480px; background: #ffffff; }
      h1 { font-family: 'Google Sans', sans-serif; font-size: 40px; padding: 24px; }
    </style>
  </head>
  <body>
    <h1>{{ page_title }}</h1>
    <p>{{ greeting }}</p>
  </body>
</html>
"""


#: The module's header widget partial. Named after the module id, because
#: that is the only name core looks for
#: (``app/modules/__init__.py:header_template_name``) and what keeps two
#: modules' partials from shadowing each other in the one flat Jinja
#: search path.
HELLO_HEADER_TEMPLATE = """\
<div class="hdr-widget">{{ header.widget.line }}</div>
"""


def write_hello_module(data_dir: Path, package: str = "hello") -> Path:
    """Install the module above under ``data_dir/modules/<package>/``."""
    root = data_dir / "modules" / package
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(HELLO_PACKAGE, encoding="utf-8")
    (root / "templates" / "hello.html").write_text(HELLO_TEMPLATE, encoding="utf-8")
    (root / "templates" / f"{package}_header.html").write_text(
        HELLO_HEADER_TEMPLATE, encoding="utf-8"
    )
    return root


#: A second module in a temp directory, this one with a settings section of
#: its own. Core has no attribute for it on ``HubSettings`` and no entry for
#: it in the module-level ``SECTIONS``, so everything it exercises - a form
#: on /settings, a POST that saves, a value that survives a reload and
#: reaches the page - has to come from the hub's own live section map.
GREETER_PACKAGE = '''\
"""A module with a settings section core has never heard of."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.modules import Module, PageSpec

HERE = Path(__file__).resolve().parent


class GreeterSettings(BaseModel):
    """What an owner sets for this module on the settings page."""

    greeting: str = Field(default="HELLO", description="Printed on the page.")
    shout: bool = Field(default=False, description="Print it in upper case.")


def greeter_context(state, settings):
    # The one accessor a module needs: it does not know or care whether its
    # section is a fixed attribute of HubSettings or a row in extra.
    own = settings.section("greeter", GreeterSettings)
    return {"greeting": own.greeting.upper() if own.shout else own.greeting}


MODULE = Module(
    id="greeter",
    title="GREETER",
    version="1.0.0",
    description="A page whose text comes from its own settings section.",
    settings_model=GreeterSettings,
    page=PageSpec(
        title="GREETER",
        templates_dir=HERE / "templates",
        template="greeter.html",
        context=greeter_context,
        render_ttl_seconds=60.0,
    ),
    default_order=95,
)
'''

GREETER_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <style>
      {{ font_css }}
      html, body { margin: 0; width: 800px; height: 480px; background: #ffffff; }
      h1 { font-family: 'Google Sans', sans-serif; font-size: 40px; padding: 24px; }
    </style>
  </head>
  <body>
    <h1>{{ page_title }}</h1>
    <p>{{ greeting }}</p>
  </body>
</html>
"""


def write_greeter_module(data_dir: Path) -> Path:
    """Install the settings-carrying module under ``data_dir/modules/greeter``."""
    root = data_dir / "modules" / "greeter"
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(GREETER_PACKAGE, encoding="utf-8")
    (root / "templates" / "greeter.html").write_text(GREETER_TEMPLATE, encoding="utf-8")
    return root


#: A third module, whose settings section declares a ``source`` field of its
#: own but is not one of ``app/settings_pages.py:TESTABLE_SECTIONS`` (only
#: built-ins are on that list): the field alone must not earn it a "Save and
#: test" button, since core has no adapter registered under "sourcey" to
#: force-fetch through.
SOURCEY_PACKAGE = '''\
"""A module whose settings section has a "source" field core cannot test."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.modules import Module, PageSpec

HERE = Path(__file__).resolve().parent


class SourceySettings(BaseModel):
    """Looks like a testable section; is not one."""

    source: str = Field(default="push", description="No adapter reads this.")


def sourcey_context(state, settings):
    return {"greeting": "hi"}


MODULE = Module(
    id="sourcey",
    title="SOURCEY",
    version="1.0.0",
    description="A settings section with a source field core cannot test.",
    settings_model=SourceySettings,
    page=PageSpec(
        title="SOURCEY",
        templates_dir=HERE / "templates",
        template="sourcey.html",
        context=sourcey_context,
        render_ttl_seconds=60.0,
    ),
    default_order=96,
)
'''

SOURCEY_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <style>
      {{ font_css }}
      html, body { margin: 0; width: 800px; height: 480px; background: #ffffff; }
      h1 { font-family: 'Google Sans', sans-serif; font-size: 40px; padding: 24px; }
    </style>
  </head>
  <body>
    <h1>{{ page_title }}</h1>
    <p>{{ greeting }}</p>
  </body>
</html>
"""


def write_sourcey_module(data_dir: Path) -> Path:
    """Install the module above under ``data_dir/modules/sourcey``."""
    root = data_dir / "modules" / "sourcey"
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(SOURCEY_PACKAGE, encoding="utf-8")
    (root / "templates" / "sourcey.html").write_text(SOURCEY_TEMPLATE, encoding="utf-8")
    return root


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _module(**overrides: object) -> Module:
    base: dict[str, object] = {
        "id": "example",
        "title": "EXAMPLE",
        "version": "1.0.0",
        "description": "An example.",
    }
    base.update(overrides)
    return Module(**base)  # type: ignore[arg-type]


def test_a_reserved_id_refuses_to_load() -> None:
    with pytest.raises(ModuleError, match="reserved"):
        validate_module(_module(id="alert"))


def test_an_all_digit_id_refuses_to_load() -> None:
    """``/display/{n}.png`` reads an all-digit segment as a page index."""
    with pytest.raises(ModuleError, match="must match"):
        validate_module(_module(id="123"))


@pytest.mark.parametrize("bad", ["Hello", "hello-world", "_hello", "", "hello world"])
def test_an_id_outside_the_pattern_refuses_to_load(bad: str) -> None:
    with pytest.raises(ModuleError, match="must match"):
        validate_module(_module(id=bad))


def test_an_id_longer_than_the_telemetry_page_column_refuses_to_load() -> None:
    """A page id is a module id verbatim, and a device echoes it back on
    every ``POST /api/device/telemetry`` (``app/models.py:DeviceTelemetry.page``,
    capped at 32 characters). An id that loads here but does not fit there
    would 400 every telemetry post from a device sitting on that page."""
    too_long = "a" + "b" * 32
    assert len(too_long) > 32
    with pytest.raises(ModuleError, match="32"):
        validate_module(_module(id=too_long))


def test_an_id_exactly_at_the_length_cap_loads() -> None:
    exactly_32 = "a" + "b" * 31
    assert len(exactly_32) == 32
    validate_module(_module(id=exactly_32))


def test_a_dataset_name_outside_the_pattern_refuses_to_load() -> None:
    spec = DatasetSpec(
        name="Not A Name",
        block_model=TasksBlock,
        value_field="items",
        section="tasks",
        build_adapter=lambda section, general, context: None,  # type: ignore[arg-type,return-value]
        ttl_seconds=lambda section: 1.0,
    )
    with pytest.raises(ModuleError, match="dataset name"):
        validate_module(_module(datasets=(spec,)))


def test_a_value_field_the_block_does_not_have_refuses_to_load() -> None:
    spec = DatasetSpec(
        name="tasks",
        block_model=TasksBlock,
        value_field="nope",
        section="tasks",
        build_adapter=lambda section, general, context: None,  # type: ignore[arg-type,return-value]
        ttl_seconds=lambda section: 1.0,
    )
    with pytest.raises(ModuleError, match="value_field"):
        validate_module(_module(datasets=(spec,)))


def _page(**overrides: object) -> PageSpec:
    base: dict[str, object] = {
        "title": "EXAMPLE",
        "templates_dir": Path("."),
        "context": lambda state, settings: {},
        "render_ttl_seconds": 60.0,
        "template": "example.html",
    }
    base.update(overrides)
    return PageSpec(**base)  # type: ignore[arg-type]


def test_a_page_with_neither_a_template_nor_a_screenshot_refuses_to_load() -> None:
    with pytest.raises(ModuleError, match="exactly one"):
        validate_module(_module(page=_page(template=None)))


def test_a_page_with_both_a_template_and_a_screenshot_refuses_to_load() -> None:
    async def shot(browser, state, settings):  # pragma: no cover - never called
        raise AssertionError

    with pytest.raises(ModuleError, match="exactly one"):
        validate_module(_module(page=_page(screenshot=shot)))


def test_demo_datasets_may_only_name_a_pushed_dataset() -> None:
    with pytest.raises(ModuleError, match="pushed dataset"):
        validate_module(_module(page=_page(demo_datasets=("weather",))))


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------
def test_two_modules_with_the_same_id_refuse_to_load() -> None:
    with pytest.raises(ModuleError, match="two modules claim the id"):
        Registry([_module(id="twin"), _module(id="twin")])


def test_two_modules_claiming_one_dataset_refuse_to_load() -> None:
    spec = DatasetSpec(
        name="tasks",
        block_model=TasksBlock,
        value_field="items",
        section="tasks",
        build_adapter=lambda section, general, context: None,  # type: ignore[arg-type,return-value]
        ttl_seconds=lambda section: 1.0,
    )
    with pytest.raises(ModuleError, match="both provide the dataset"):
        Registry([_module(id="one", datasets=(spec,)), _module(id="two", datasets=(spec,))])


class _DummySettings(BaseModel):
    """A settings model with nothing in it: only its section name matters
    to the tests below."""


def test_a_settings_section_reserved_for_core_refuses_to_load() -> None:
    with pytest.raises(ModuleError, match="general.*reserved for core"):
        validate_module(
            _module(settings_model=_DummySettings, settings_section="general")
        )


@pytest.mark.parametrize("section", ["general", "device", "alert", "modules"])
def test_every_reserved_section_refuses_to_load(section: str) -> None:
    with pytest.raises(ModuleError, match="reserved for core"):
        validate_module(
            _module(settings_model=_DummySettings, settings_section=section)
        )


def test_two_modules_claiming_the_same_settings_section_refuse_to_load() -> None:
    with pytest.raises(ModuleError, match="both own the settings section 'shared'"):
        Registry(
            [
                _module(id="one", settings_model=_DummySettings, settings_section="shared"),
                _module(id="two", settings_model=_DummySettings, settings_section="shared"),
            ]
        )


def test_a_third_party_module_claiming_a_builtin_section_refuses_to_load() -> None:
    """Weather is not in RESERVED_SECTIONS - it belongs to the built-in
    weather module, not core - so this has to be caught as a duplicate
    section owner, not a reserved-word refusal, and the message has to name
    both modules."""
    modules = [
        *builtin_modules(),
        _module(id="rival", settings_model=_DummySettings, settings_section="weather"),
    ]
    with pytest.raises(ModuleError, match="both own the settings section 'weather'"):
        Registry(modules)


# ---------------------------------------------------------------------------
# a module's own screenshot renderer (app/renderer/render.py)
# ---------------------------------------------------------------------------
async def _oversized_screenshot(browser: Any, state: Any, settings: Any) -> Image.Image:
    """A ``ScreenshotFn`` that ignores everything it is handed and returns an
    image the wrong size, the way a careless third-party module might."""
    return Image.new("RGB", (1024, 600), "white")


def test_a_module_screenshot_the_wrong_size_is_forced_to_the_display_size(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """The template path always hands back exactly ``DISPLAY_SIZE``
    (Chromium's own clip guarantees it, and a mismatch there is already a
    warning); a module's ``ScreenshotFn`` is arbitrary code with no such
    guarantee and used to be trusted after nothing but ``convert("RGB")``.
    It must not silently misdraw the panel - it gets resized, loudly."""
    module = _module(
        id="oversized",
        page=_page(template=None, screenshot=_oversized_screenshot),
    )
    renderer = Renderer(env, HubSettings(), Registry([module]))
    state = make_state(
        generated_at=datetime(2026, 9, 20, 9, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    )
    try:
        run(renderer.start())
        with caplog.at_level(logging.WARNING, logger="app.render"):
            image = run(renderer.render_rgb("oversized", state))
    finally:
        run(renderer.close())

    assert image.size == DISPLAY_SIZE
    assert any(
        getattr(record, "fields", {}).get("page") == "oversized"
        and getattr(record, "fields", {}).get("actual") == (1024, 600)
        for record in caplog.records
    )


def test_the_built_in_pages_keep_their_ids_and_their_order() -> None:
    """A flashed device asks for ``/display/today.png`` by name; these five
    strings and their order are part of that contract."""
    assert builtin_registry().page_ids() == ("today", "agenda", "weather", "brief", "system")


def test_the_built_in_datasets_are_the_seven_the_hub_has_always_had() -> None:
    assert set(builtin_registry().datasets()) == {
        "tasks",
        "calendar",
        "weather",
        "ai_usage",
        "brief",
        "home",
        "device",
    }


def test_settings_order_overrides_the_default_order() -> None:
    settings = ModulesSettings(items=[ModuleToggle(id="system", order=1)])
    assert builtin_registry(settings).page_ids()[0] == "system"


def test_a_disabled_module_keeps_its_settings_section() -> None:
    """Turning a module off must not lose what its section holds."""
    settings = ModulesSettings(items=[ModuleToggle(id="weather", enabled=False)])
    registry = builtin_registry(settings)
    assert "weather" not in registry.page_ids()
    assert "weather" in registry.sections()


def test_an_id_in_settings_that_is_not_installed_is_reported_not_dropped() -> None:
    settings = ModulesSettings(items=[ModuleToggle(id="ghost", enabled=False)])
    assert builtin_registry(settings).missing_ids() == ("ghost",)


def test_toggle_rows_show_the_manifest_defaults_for_a_module_with_no_row() -> None:
    """The settings form lists what is in force, not what is stored: a
    module nobody has touched still shows its own enabled state and order."""
    rows = {row.id: row for row in builtin_registry().toggle_rows()}
    assert rows["today"].enabled is True
    assert rows["today"].order == 10


def test_toggle_rows_keep_a_row_for_a_module_that_is_not_installed() -> None:
    """Dropping it from the form would delete it on the next save."""
    settings = ModulesSettings(items=[ModuleToggle(id="ghost", enabled=False, order=7)])
    rows = builtin_registry(settings).toggle_rows()
    ghost = [row for row in rows if row.id == "ghost"]
    assert ghost == [ModuleToggle(id="ghost", enabled=False, order=7)]
    # Last, after every installed module.
    assert rows[-1].id == "ghost"


def test_with_settings_applies_a_submission_without_saving_it() -> None:
    """What the settings page checks a modules save against before writing."""
    registry = builtin_registry()
    disabled = ModulesSettings(
        items=[ModuleToggle(id=page_id, enabled=False) for page_id in registry.page_ids()]
    )
    assert registry.with_settings(disabled).pages() == ()
    # The registry it was asked of is untouched.
    assert registry.page_ids()


def test_page_by_index_resolves_to_the_id_and_stops_at_the_end() -> None:
    registry = builtin_registry()
    first = registry.page_by_index(0)
    assert first is not None and first.id == "today"
    assert registry.page_by_index(len(registry.pages())) is None
    assert registry.page_by_index(-1) is None


# ---------------------------------------------------------------------------
# a module installed into DATA_DIR/modules/
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def hello_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data_dir = tmp_path_factory.mktemp("hello-data")
    write_hello_module(data_dir)
    return data_dir


@pytest.fixture(scope="module")
def hello_client(hello_data_dir: Path) -> Iterator[TestClient]:
    env = Env(
        _env_file=None,
        DATA_DIR=hello_data_dir,
        LOG_LEVEL="WARNING",
    )
    app = create_app(env, HubSettings())
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def hello_token(hello_client: TestClient) -> str:
    secrets = asyncio.run(
        hello_client.app.state.hub.identity.claim(
            name="deskmate", base_url="http://dashboard-hub.lan:8080"
        )
    )
    return secrets.token


def test_a_module_from_the_data_directory_is_a_page(hello_client: TestClient) -> None:
    registry = hello_client.app.state.hub.registry
    assert "hello" in registry.page_ids()
    # Default order 90 puts it after the five built-ins and before nothing.
    assert registry.page_ids()[-1] == "hello"
    assert "greeting" in registry.datasets()


def test_a_module_from_the_data_directory_is_in_the_window_list(
    hello_client: TestClient, hello_token: str
) -> None:
    page = hello_client.get("/preview/today.html", headers=auth(hello_token))
    assert page.status_code == 200
    assert "HELLO" in page.text


def test_a_module_from_the_data_directory_renders_its_own_page(
    hello_client: TestClient, hello_token: str
) -> None:
    html = hello_client.get("/preview/hello.html", headers=auth(hello_token))
    assert html.status_code == 200
    assert "HELLO FROM A THIRD PARTY" in html.text

    png = hello_client.get("/display/hello.png", headers=auth(hello_token))
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert png.headers["x-deskmate-page"] == "hello"


def test_healthz_lists_the_module_page_and_its_dataset(
    hello_client: TestClient, hello_token: str
) -> None:
    payload = hello_client.get("/healthz", headers=auth(hello_token)).json()
    assert "hello" in payload["pages"]
    assert "greeting" in payload["adapters"]


def test_disabling_a_module_removes_its_page_after_reload(
    hello_client: TestClient, hello_token: str
) -> None:
    hub = hello_client.app.state.hub
    store = hub.settings_store
    before = store.load("modules")
    try:
        store.save("modules", ModulesSettings(items=[ModuleToggle(id="hello", enabled=False)]))
        asyncio.run(hub.reload())

        assert "hello" not in hub.registry.page_ids()
        assert "greeting" not in hub.state_service.adapters
        assert hello_client.get("/display/hello.png", headers=auth(hello_token)).status_code == 404
        today = hello_client.get("/preview/today.html", headers=auth(hello_token))
        assert "HELLO" not in today.text
    finally:
        store.save("modules", before)
        asyncio.run(hub.reload())
    assert "hello" in hub.registry.page_ids()


def test_a_directory_module_can_fill_the_header_widget_slot(
    hello_client: TestClient, hello_token: str
) -> None:
    """A module installed by dropping a directory in brings a header widget
    like any built-in: it is offered on the settings page, and picking it
    draws its partial into the header of a page core owns
    (docs/MODULES.md, "Header widget")."""
    hub = hello_client.app.state.hub
    assert "hello" in [module.id for module in hub.registry.installed_header_widgets()]
    before = hub.settings_store.load("general")
    try:
        hub.settings_store.save(
            "general", before.model_copy(update={"header_widget": "hello"})
        )
        run(hub.reload())
        page = hello_client.get("/preview/today.html", headers=auth(hello_token))
        assert page.status_code == 200
        assert "HELLO FROM THE HEADER" in page.text
    finally:
        hub.settings_store.save("general", before)
        run(hub.reload())


def test_a_module_directory_without_an_init_is_ignored(hello_data_dir: Path) -> None:
    """Half an installation is not an installation."""
    (hello_data_dir / "modules" / "not-a-package").mkdir(exist_ok=True)
    ids = {module.id for module in directory_modules(hello_data_dir)}
    assert ids == {"hello"}


def test_a_module_directory_shadowing_an_installed_package_refuses_to_load(
    tmp_path: Path,
) -> None:
    """``DATA_DIR/modules/calendar/`` used to replace the standard library's
    own ``calendar`` module for the rest of the process: ``directory_modules``
    put the data directory at the front of ``sys.path`` and evicted whatever
    was already in ``sys.modules`` under that name before importing its own,
    so ``import calendar`` anywhere else in the process returned the dropped
    package instead. It has to refuse instead, and it has to do so without
    ever touching the real ``calendar`` module."""
    import calendar as stdlib_calendar

    month_name = stdlib_calendar.month_name

    data_dir = tmp_path / "shadow"
    write_hello_module(data_dir, package="calendar")

    with pytest.raises(ModuleError, match="calendar.*shadows an installed package"):
        directory_modules(data_dir)

    assert "calendar" in sys.modules
    assert sys.modules["calendar"] is stdlib_calendar
    assert stdlib_calendar.month_name is month_name


# ---------------------------------------------------------------------------
# a module route colliding with a core path (app/main.py:create_app)
# ---------------------------------------------------------------------------
#: A module whose push route is "/alert" - the same path core's own
#: POST/DELETE /api/alert answers, once mounted under the shared "/api"
#: prefix every module router gets. Written to disk (not built in Python
#: and handed to a Registry directly) because the collision check lives in
#: create_app's own route-mounting loop, which only ever sees modules the
#: normal discovery paths found.
SHADOW_ALERT_PACKAGE = '''\
"""A module that declares a route colliding with a core path."""

from __future__ import annotations

from fastapi import APIRouter

from app.modules import Module, ModuleContext


def _routes(context: ModuleContext) -> APIRouter:
    router = APIRouter()

    @router.post("/alert")
    async def shadow_alert() -> dict:
        return {"shadowed": True}

    return router


MODULE = Module(
    id="shadow",
    title="SHADOW",
    version="1.0.0",
    description="Declares POST /alert, which core already owns under /api.",
    routes=_routes,
)
'''


def write_shadow_alert_module(data_dir: Path) -> Path:
    root = data_dir / "modules" / "shadow"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(SHADOW_ALERT_PACKAGE, encoding="utf-8")
    return root


def test_a_module_route_colliding_with_a_core_path_refuses_to_load(tmp_path: Path) -> None:
    """Module routers used to be mounted before core's own /api/alert and
    /api/device/telemetry routes were even declared, so a module route of
    the same path would shadow core's - Starlette matches in declaration
    order - silently, for as long as the process stayed up. core's routes
    are declared first now, and this is the belt: a collision refuses to
    load at all rather than merely losing the ordering race the other way."""
    data_dir = tmp_path / "shadow-route"
    write_shadow_alert_module(data_dir)
    env = Env(_env_file=None, DATA_DIR=data_dir, LOG_LEVEL="WARNING")

    try:
        with pytest.raises(ModuleError, match="POST /api/alert.*collides with a core path"):
            create_app(env)
    finally:
        get_database(env.hub_db_file).close()


def test_an_ordinary_module_route_still_mounts_and_answers(tmp_path: Path) -> None:
    """The sanity check the refusal above needs: a module route that does
    not collide with anything still mounts under /api and still answers, so
    the fix is a refusal for a real collision, not routers failing to mount
    in general once core's own routes come first."""
    data_dir = tmp_path / "harmless-route"
    root = data_dir / "modules" / "harmless"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text(
        textwrap.dedent(
            '''\
            from __future__ import annotations

            from fastapi import APIRouter
            from fastapi.responses import JSONResponse

            from app.modules import Module, ModuleContext


            def _routes(context: ModuleContext) -> APIRouter:
                router = APIRouter()

                @router.post("/harmless")
                async def harmless() -> JSONResponse:
                    return JSONResponse({"ok": True})

                return router


            MODULE = Module(
                id="harmless",
                title="HARMLESS",
                version="1.0.0",
                description="A push route with no collision.",
                routes=_routes,
            )
            '''
        ),
        encoding="utf-8",
    )
    env = Env(_env_file=None, DATA_DIR=data_dir, LOG_LEVEL="WARNING")
    app = create_app(env)
    try:
        secrets = asyncio.run(
            app.state.hub.identity.claim(
                name="deskmate", base_url="http://dashboard-hub.lan:8080"
            )
        )
        client = TestClient(app)
        response = client.post(
            "/api/harmless", headers={"Authorization": f"Bearer {secrets.token}"}
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    finally:
        app.state.hub.db.close()


# ---------------------------------------------------------------------------
# reload publishes registry, state_service and renderer together
# (app/main.py:Hub.reload)
# ---------------------------------------------------------------------------
def _same_generation(hub: "Hub") -> bool:
    """Whether ``hub.registry``, its state service's own registry and its
    renderer's own registry are literally the same object.

    ``Hub._rebuild`` used to assign ``self.registry`` from inside the
    threadpool call ``reload`` awaits it through, while ``state_service`` and
    ``renderer`` were only rebuilt after that call returned: a request
    landing on the event loop in that window read a new registry paired with
    an old state service, so a page a settings save had just enabled was a
    KeyError there instead of a render. Identity (``is``), not equality: two
    separately-built registries over the same settings could compare equal
    without being the one object every reader is holding.
    """
    return (
        hub.registry is hub.state_service._registry  # noqa: SLF001 - the whole point of this check
        and hub.registry is hub.renderer.registry
    )


def test_reload_leaves_registry_state_service_and_renderer_in_step(tmp_path: Path) -> None:
    env = Env(_env_file=None, DATA_DIR=tmp_path / "reload-step", LOG_LEVEL="WARNING")
    hub = Hub(env)
    try:
        assert _same_generation(hub)
        before = hub.registry

        hub.settings_store.save(
            "modules", ModulesSettings(items=[ModuleToggle(id="weather", enabled=False)])
        )
        run(hub.reload())

        assert _same_generation(hub)
        # And it really did rebuild, not just re-check the same objects.
        assert hub.registry is not before
    finally:
        hub.db.close()


def test_reload_never_publishes_a_torn_pair_under_a_slow_rebuild(tmp_path: Path) -> None:
    """The regression this closes: a concurrent reader on the event loop
    must never see a new registry paired with an old state service or
    renderer while ``_rebuild`` is still running in its threadpool.

    ``_rebuild`` is monkeypatched to sleep *after* doing its real work but
    *before* returning, which is exactly the window the old code's
    assignment of ``self.registry`` from inside that call would have opened:
    if this were still assigning eagerly, ``hub.registry`` would already be
    the new one here while ``hub.state_service``/``hub.renderer`` were still
    the old ones. A background task samples ``_same_generation`` on every
    loop iteration for the duration of the sleep; the assertion is that
    every single sample agreed, not just the ones before and after.
    """
    env = Env(_env_file=None, DATA_DIR=tmp_path / "reload-torn", LOG_LEVEL="WARNING")
    hub = Hub(env)
    try:
        real_rebuild = Hub._rebuild

        def slow_rebuild(self: Hub, seed: HubSettings | None = None) -> Any:
            result = real_rebuild(self, seed)
            time.sleep(0.2)
            return result

        async def scenario() -> list[bool]:
            samples: list[bool] = []

            async def sample_while_reloading() -> None:
                for _ in range(50):
                    samples.append(_same_generation(hub))
                    await asyncio.sleep(0.005)

            hub.settings_store.save(
                "modules", ModulesSettings(items=[ModuleToggle(id="weather", enabled=False)])
            )
            with mock.patch.object(Hub, "_rebuild", slow_rebuild):
                await asyncio.gather(hub.reload(), sample_while_reloading())
            return samples

        samples = run(scenario())
        assert samples, "the sampler never got a chance to run"
        assert all(samples)
        assert _same_generation(hub)
    finally:
        hub.db.close()


# ---------------------------------------------------------------------------
# the modules settings section through the generated form
# ---------------------------------------------------------------------------
def test_the_modules_section_round_trips_through_the_settings_form() -> None:
    """The section is a list of scalars, which is exactly what the form
    generator renders (``app/forms.py``). 2.1b puts the section on the page;
    this is the claim that it will fit when it does."""
    current = ModulesSettings(
        items=[
            ModuleToggle(id="today", enabled=True, order=10),
            ModuleToggle(id="weather", enabled=False, order=None),
        ]
    )
    form = render_section("modules", ModulesSettings, current.model_dump())
    parsed = parse_section(ModulesSettings, FormData(submission(form)), current)

    assert parsed.errors == {}
    assert ModulesSettings.model_validate(parsed.data) == current


def test_the_example_package_is_plain_ascii() -> None:
    """The repo's own rule, asserted where a test writes source to disk."""
    assert textwrap.dedent(HELLO_PACKAGE).isascii()
    assert HELLO_TEMPLATE.isascii()
    assert textwrap.dedent(GREETER_PACKAGE).isascii()
    assert GREETER_TEMPLATE.isascii()
    assert textwrap.dedent(SOURCEY_PACKAGE).isascii()
    assert SOURCEY_TEMPLATE.isascii()


# ---------------------------------------------------------------------------
# a third-party module's own settings section
# ---------------------------------------------------------------------------
@pytest.fixture()
def greeter(tmp_path: Path) -> Iterator[AdminHub]:
    """A claimed hub with the greeter module installed in its DATA_DIR."""
    data_dir = tmp_path / "live"
    write_greeter_module(data_dir)
    hub = AdminHub(data_dir)
    yield hub
    hub.hub.db.close()


def greeter_settings(admin: AdminHub) -> Any:
    """The greeter section as the hub holds it right now."""
    return admin.hub.hub_settings.extra["greeter"]


def save_greeting(admin: AdminHub, **fields: str) -> Any:
    return admin.client.post(
        "/settings/greeter",
        data={"action": "save", **fields},
        headers=admin.auth,
        follow_redirects=False,
    )


def test_a_third_party_section_is_in_the_hubs_section_map(greeter: AdminHub) -> None:
    """``SECTIONS`` is fixed at import and cannot know this module exists."""
    assert "greeter" not in SECTIONS
    assert "greeter" in greeter.hub.sections
    assert greeter_settings(greeter).greeting == "HELLO"


def test_a_third_party_section_renders_its_own_form(greeter: AdminHub) -> None:
    page = greeter.client.get("/settings", headers=greeter.auth)
    assert page.status_code == 200
    assert 'id="greeter"' in page.text
    assert 'action="/settings/greeter"' in page.text
    assert 'id="greeter-greeting"' in page.text
    assert 'id="greeter-shout"' in page.text


def test_a_third_party_section_saves_and_survives_a_reload(greeter: AdminHub) -> None:
    assert save_greeting(greeter, greeting="bonjour", shout="1").status_code == 303
    assert greeter_settings(greeter).greeting == "bonjour"
    assert greeter_settings(greeter).shout is True

    asyncio.run(greeter.hub.reload())
    assert greeter_settings(greeter).greeting == "bonjour"
    # And it is on the page the admin comes back to, not only in memory.
    assert 'value="bonjour"' in greeter.client.get("/settings", headers=greeter.auth).text


def test_a_third_party_section_reaches_its_own_page(greeter: AdminHub) -> None:
    """The point of the whole exercise: what was saved is what is drawn."""
    save_greeting(greeter, greeting="bonjour", shout="1")
    state = make_state(generated_at=datetime(2026, 9, 20, 9, 0, tzinfo=ZoneInfo("Asia/Bangkok")))
    html = greeter.hub.renderer.render_html("greeter", state, embed_fonts=False)
    assert "BONJOUR" in html


def test_a_disabled_third_party_module_keeps_its_section(greeter: AdminHub) -> None:
    """Turning a module off must not lose what its section holds, and must
    not take its form off the page either: there would be no way back."""
    save_greeting(greeter, greeting="bonjour")
    greeter.hub.settings_store.save(
        "modules", ModulesSettings(items=[ModuleToggle(id="greeter", enabled=False)])
    )
    asyncio.run(greeter.hub.reload())

    assert "greeter" not in greeter.hub.registry.page_ids()
    assert "greeter" in greeter.hub.sections
    assert greeter_settings(greeter).greeting == "bonjour"
    page = greeter.client.get("/settings", headers=greeter.auth)
    assert 'action="/settings/greeter"' in page.text


# ---------------------------------------------------------------------------
# "Save and test" only for sections core can actually test
# ---------------------------------------------------------------------------
@pytest.fixture()
def sourcey(tmp_path: Path) -> Iterator[AdminHub]:
    """A claimed hub with a module whose settings model has a ``source``
    field, but whose section is not in ``TESTABLE_SECTIONS``."""
    data_dir = tmp_path / "live"
    write_sourcey_module(data_dir)
    hub = AdminHub(data_dir)
    yield hub
    hub.hub.db.close()


def _section_html(page_text: str, section: str) -> str:
    """The one ``<section id="...">...</section>`` block for ``section``,
    from a rendered /settings page: slicing it out is what lets a test say
    "this section's button", not just "the button is on the page somewhere"."""
    start = page_text.index(f'id="{section}"')
    return page_text[start : page_text.index("</section>", start)]


def test_a_source_field_alone_is_not_testable(sourcey: AdminHub) -> None:
    """``source`` is necessary but not sufficient: ``sourcey`` is not on
    ``TESTABLE_SECTIONS`` (app/settings_pages.py), so its section renders
    with no adapter core could force-fetch through, and so no button."""
    assert "sourcey" not in TESTABLE_SECTIONS
    page = sourcey.client.get("/settings", headers=sourcey.auth)
    assert page.status_code == 200
    assert 'action="/settings/sourcey"' in page.text
    assert "Save and test" not in _section_html(page.text, "sourcey")


def test_a_testable_builtin_section_still_gets_its_button(sourcey: AdminHub) -> None:
    """The fix for the case above must not cost weather (a real
    ``TESTABLE_SECTIONS`` member) its own button."""
    assert "weather" in TESTABLE_SECTIONS
    page = sourcey.client.get("/settings", headers=sourcey.auth)
    assert "Save and test" in _section_html(page.text, "weather")
