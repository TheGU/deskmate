"""The module API and the registry: what loads, what refuses, what shows up.

The interesting case is a module nobody shipped: a package dropped into
``DATA_DIR/modules/``, with its own settings-free page, its own template
directory and its own dataset. If that reaches the window list, the render
and ``/display/<id>.png`` without core knowing its name, the contract in
docs/plan/2026-09-19-settings-modules-provisioning.md (phase 2) holds.
"""

from __future__ import annotations

import asyncio
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from app.config import REPO_ROOT, Env
from app.forms import parse_section, render_section
from app.main import create_app
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
    builtin_registry,
    directory_modules,
)
from app.settings import SECTIONS, HubSettings
from tests.conftest import make_state
from tests.test_forms import submission
from tests.test_settings_page import AdminHub

FIXTURES_DIR = REPO_ROOT / "fixtures"

#: A whole module in one file, written into ``DATA_DIR/modules/hello/``. It
#: brings a page with its own template directory and one dataset whose
#: adapter needs nothing at all, so what the test proves is the wiring, not
#: the module's own cleverness.
HELLO_PACKAGE = '''\
"""A module that exists only in a test's temp directory."""

from __future__ import annotations

from pathlib import Path

from app.models import Task, TasksBlock
from app.modules import DatasetSpec, Module, PageSpec

HERE = Path(__file__).resolve().parent


class GreetingAdapter:
    name = "greeting"
    source = "static"

    async def fetch(self) -> list[Task]:
        return [Task(id="hello-1", title="Say hello")]


def greeting_context(state, settings):
    return {"greeting": "HELLO FROM A THIRD PARTY"}


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


def write_hello_module(data_dir: Path, package: str = "hello") -> Path:
    """Install the module above under ``data_dir/modules/<package>/``."""
    root = data_dir / "modules" / package
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(HELLO_PACKAGE, encoding="utf-8")
    (root / "templates" / "hello.html").write_text(HELLO_TEMPLATE, encoding="utf-8")
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
        FIXTURES_DIR=FIXTURES_DIR,
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


def test_a_module_directory_without_an_init_is_ignored(hello_data_dir: Path) -> None:
    """Half an installation is not an installation."""
    (hello_data_dir / "modules" / "not-a-package").mkdir(exist_ok=True)
    ids = {module.id for module in directory_modules(hello_data_dir)}
    assert ids == {"hello"}


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
