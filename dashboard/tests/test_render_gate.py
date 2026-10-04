"""PNG sha256 gate: the six panel pages rendered from a frozen fixture state
must stay byte-identical across the settings/modules/provisioning refactors
(docs/plan/2026-09-19-settings-modules-provisioning.md, Verification).

The frozen state and its expected hashes are produced by
``tests/assets/freeze_state.py``. If a page's hash changes here and the change
to the rendered pixels is intended, rerun that script to refresh
the platform's ``tests/assets/frozen-hashes*.json`` (and, if fixture data or state
shape changed, ``tests/assets/frozen-state.json`` too); if it was not
intended, this failure names the regression to fix.
"""

from __future__ import annotations

import hashlib
import json
import sys
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


def hashes_path(platform: str) -> Path:
    """Keep exact gates for each reviewed Chromium rasterization platform.

    Matching browser and bundled fonts still rasterize text differently on
    Windows and Linux. Never silently fall back to another platform's gate.
    """
    filenames = {"win32": "frozen-hashes.json", "linux": "frozen-hashes-linux.json"}
    if platform not in filenames:
        raise ValueError(f"no reviewed render hashes for platform {platform!r}")
    return ASSETS_DIR / filenames[platform]


HASHES_PATH = hashes_path(sys.platform)

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


@pytest.mark.parametrize("platform,filename", [
    ("win32", "frozen-hashes.json"), ("linux", "frozen-hashes-linux.json"),
])
def test_hashes_are_selected_for_the_actual_rendering_platform(platform: str, filename: str) -> None:
    assert hashes_path(platform) == ASSETS_DIR / filename


def test_unknown_rendering_platform_has_no_fallback_baseline() -> None:
    with pytest.raises(ValueError, match="no reviewed render hashes"):
        hashes_path("unreviewed")


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
        "tests/assets/freeze_state.py --hashes-only only if this pixel change is "
        "intended; otherwise this is a rendering regression."
    )


def test_hashes_only_regeneration_preserves_shared_state_and_other_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.assets import freeze_state as freezer

    payload = STATE_PATH.read_bytes()
    shared_state = tmp_path / "frozen-state.json"
    shared_state.write_bytes(payload)
    other_hashes = tmp_path / "other-platform.json"
    other_hashes.write_text("other platform must remain unchanged", encoding="utf-8")
    target = tmp_path / "this-platform.json"
    monkeypatch.setattr(freezer, "STATE_PATH", shared_state)
    monkeypatch.setattr(freezer, "HASHES_PATH", target)

    async def render(env: object, settings: object, state: DashboardState) -> dict[str, str]:
        assert state == load_frozen_state(payload.decode("utf-8"))
        return {page: "reviewed digest" for page in PAGES}

    monkeypatch.setattr(freezer, "_render_hashes", render)
    assert run(freezer._write_hashes_only()) == 0
    assert shared_state.read_bytes() == payload
    assert other_hashes.read_text(encoding="utf-8") == "other platform must remain unchanged"
    assert json.loads(target.read_text(encoding="utf-8")) == {page: "reviewed digest" for page in PAGES}
