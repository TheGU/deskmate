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

from app.models import DashboardState
from app.renderer.render import PAGES, Renderer
from tests.conftest import run

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
STATE_PATH = ASSETS_DIR / "frozen-state.json"
HASHES_PATH = ASSETS_DIR / "frozen-hashes.json"


@pytest.fixture(scope="module")
def frozen_state() -> DashboardState:
    return DashboardState.model_validate_json(STATE_PATH.read_text(encoding="utf-8"))


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
