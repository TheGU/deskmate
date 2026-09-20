"""examples/modules/hello installs the way docs/MODULES.md says it does.

This copies the actual example directory (not a copy of its source pasted
into the test, the way ``tests/test_modules.py``'s inline ``HELLO_PACKAGE``
does) into a temp ``DATA_DIR/modules/``, boots a hub over it and checks the
three things a developer following the doc would expect: the module is
installed, its page is in the footer window list, and it renders a real
800x480 PNG.
"""

from __future__ import annotations

import asyncio
import io
import shutil
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import REPO_ROOT, Env
from app.main import create_app
from app.renderer.palette import DISPLAY_SIZE
from app.settings import HubSettings

EXAMPLE_HELLO = REPO_ROOT / "examples" / "modules" / "hello"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def hello_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data_dir = tmp_path_factory.mktemp("example-hello-data")
    shutil.copytree(EXAMPLE_HELLO, data_dir / "modules" / "hello")
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


def test_the_example_module_is_installed(hello_client: TestClient) -> None:
    registry = hello_client.app.state.hub.registry
    assert "hello" in registry.page_ids()


def test_the_example_module_is_in_the_footer_window_list(
    hello_client: TestClient, hello_token: str
) -> None:
    # today.html is a page core already draws; its rendered footer is the
    # window list every enabled page shares, so hello showing up there is
    # the proof that installing the example is enough to see it navigated
    # to, not just registered.
    page = hello_client.get("/preview/today.html", headers=auth(hello_token))
    assert page.status_code == 200
    assert "HELLO" in page.text


def test_the_example_module_renders_an_800x480_png(
    hello_client: TestClient, hello_token: str
) -> None:
    png = hello_client.get("/display/hello.png", headers=auth(hello_token))
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert png.headers["x-deskmate-page"] == "hello"
    image = Image.open(io.BytesIO(png.content))
    image.verify()
    assert Image.open(io.BytesIO(png.content)).size == DISPLAY_SIZE
