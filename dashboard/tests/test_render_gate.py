"""PNG sha256 gate: the six panel pages rendered from a frozen fixture state
must stay byte-identical across the settings/modules/provisioning refactors
(docs/plan/2026-09-19-settings-modules-provisioning.md, Verification).

The frozen state and its expected hashes are produced by
``tests/assets/freeze_state.py``. If a page's hash changes here and the change
to the rendered pixels is intended, rerun that script to refresh
``tests/assets/frozen-hashes.json`` (and, if the fixture data or the state
shape changed, ``tests/assets/frozen-state.json`` too); if it was not
intended, this failure names the regression to fix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models import (
    AIUsageBlock,
    Block,
    BriefBlock,
    CalendarBlock,
    DashboardState,
    DeviceBlock,
    HomeBlock,
    TasksBlock,
    WeatherBlock,
)
from app.renderer.render import PAGES, Renderer
from tests.conftest import run

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
STATE_PATH = ASSETS_DIR / "frozen-state.json"
HASHES_PATH = ASSETS_DIR / "frozen-hashes.json"

#: The seven typed block fields ``DashboardState`` carried at the top level
#: until 2.1a turned them into the ``blocks`` mapping. The committed frozen
#: state is in that older shape on purpose: regenerating it would also
#: regenerate the hashes, and a gate that rewrites its own expectation is
#: not a gate. So the loader below lifts the old keys into ``blocks``
#: instead, which is a pure rename of where each block sits, not a change
#: to a single value inside one.
LEGACY_BLOCK_MODELS: dict[str, type[Block]] = {
    "tasks": TasksBlock,
    "calendar": CalendarBlock,
    "weather": WeatherBlock,
    "ai_usage": AIUsageBlock,
    "brief": BriefBlock,
    "home": HomeBlock,
    "device": DeviceBlock,
}


def load_frozen_state(payload: str) -> DashboardState:
    """The committed frozen state, in whichever shape it is written in."""
    document = json.loads(payload)
    if "blocks" not in document:
        document["blocks"] = {
            name: document.pop(name)
            for name in list(LEGACY_BLOCK_MODELS)
            if name in document
        }
    blocks = {
        name: LEGACY_BLOCK_MODELS[name].model_validate(value)
        if name in LEGACY_BLOCK_MODELS
        else Block.model_validate(value)
        for name, value in document.pop("blocks").items()
    }
    return DashboardState.model_validate({**document, "blocks": {}}).model_copy(
        update={"blocks": blocks}
    )


@pytest.fixture(scope="module")
def frozen_state() -> DashboardState:
    return load_frozen_state(STATE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def frozen_hashes() -> dict[str, str]:
    return json.loads(HASHES_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("page", PAGES)
def test_page_png_matches_frozen_hash(
    renderer: Renderer,
    frozen_state: DashboardState,
    frozen_hashes: dict[str, str],
    page: str,
) -> None:
    png = run(renderer.render_png(page, frozen_state))
    digest = hashlib.sha256(png).hexdigest()
    expected = frozen_hashes[page]
    assert digest == expected, (
        f"{page!r} page PNG no longer matches its frozen hash "
        f"(got {digest}, expected {expected}). Rerun "
        "tests/assets/freeze_state.py only if this change to the pixels is "
        "intended; otherwise this is a rendering regression."
    )
