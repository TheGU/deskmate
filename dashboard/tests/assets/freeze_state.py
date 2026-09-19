"""Builds the frozen fixture state and its PNG hashes for the render gate.

Run from ``dashboard/``::

    uv run python tests/assets/freeze_state.py

This writes two files next to this script:

``frozen-state.json``
    ``DashboardState.model_dump_json`` of the same fixture state
    ``tests/conftest.py``'s session ``state`` fixture builds (every
    ``*_SOURCE=fixture``, a temp ``DATA_DIR``, ``TIMEZONE=Asia/Bangkok``), but
    with ``FIXTURE_RELATIVE_DATES=false`` (so the fixture dates are the
    literal dates in the fixture files, not shifted to "today") and with
    every block's ``updated_at`` and the state's own ``generated_at`` pinned
    to one fixed moment, so nothing in the frozen state depends on when this
    script happens to run.

``frozen-hashes.json``
    Maps each page in ``app.renderer.render.PAGES`` to the sha256 of its
    rendered PNG bytes, rendered from the frozen state above.

``dashboard/tests/test_render_gate.py`` re-renders every page from
``frozen-state.json`` and asserts its hash still matches ``frozen-hashes.json``.
Re-run this script only when a change to the rendered pixels is intended;
otherwise a mismatch is a regression to fix, not a hash to refresh.

Note: this script is not itself idempotent across runs. The device block's
fixture (app/adapters/device.py's ``load_device_fixture``) deliberately
generates its 24 h history relative to wall-clock "now" ("a fixed anchor date
would make the demo device look permanently stale", per its own docstring),
so the ``device``/``system`` page content can differ between two invocations
of this script even though every timestamp on ``DashboardState`` itself is
pinned above. That is fine for what this script is for: it freezes one
snapshot to disk once, and everything downstream (the render gate) only ever
renders that already-frozen, unchanging state, which is what makes the gate
itself byte-for-byte reproducible.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

#: dashboard/ - the directory ``app`` and ``tests`` import from. Needed
#: because running this file directly (not through pytest, which sets
#: pythonpath via pyproject.toml) only puts this script's own directory on
#: sys.path.
DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from app.alerts import AlertStore  # noqa: E402
from app.config import REPO_ROOT, Settings  # noqa: E402
from app.models import DashboardState  # noqa: E402
from app.renderer.render import PAGES, Renderer  # noqa: E402
from app.state import StateService  # noqa: E402
from app.telemetry import close_telemetry_stores  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "fixtures"
ASSETS_DIR = Path(__file__).resolve().parent
STATE_PATH = ASSETS_DIR / "frozen-state.json"
HASHES_PATH = ASSETS_DIR / "frozen-hashes.json"

#: The one moment every block's ``updated_at`` and the state's ``generated_at``
#: are pinned to. Chosen as a plain, readable Asia/Bangkok morning; the exact
#: value carries no meaning beyond being fixed.
FROZEN_AT = datetime.fromisoformat("2026-09-19T09:00:00+07:00")


def _settings(data_dir: Path) -> Settings:
    """Same fixture settings as ``tests/conftest.py``'s session fixture,
    except ``FIXTURE_RELATIVE_DATES=False``: the gate wants the literal
    fixture dates, not dates shifted to whatever day this script runs on.
    """
    return Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        FIXTURE_RELATIVE_DATES=False,
        LOG_LEVEL="WARNING",
        TASKS_SOURCE="fixture",
        CALENDAR_SOURCE="fixture",
        WEATHER_SOURCE="fixture",
        AI_USAGE_SOURCE="fixture",
        BRIEF_SOURCE="fixture",
        HA_SOURCE="fixture",
        DEVICE_SOURCE="fixture",
    )


def _freeze_timestamps(state: DashboardState) -> DashboardState:
    """Pin every block's ``updated_at`` and the state's ``generated_at`` to
    :data:`FROZEN_AT`, so nothing in the frozen state depends on wall-clock
    time at freeze time."""
    frozen_blocks = {
        name: block.model_copy(update={"updated_at": FROZEN_AT})
        for name, block in state.blocks.items()
    }
    return state.model_copy(update={"generated_at": FROZEN_AT, **frozen_blocks})


async def _build_frozen_state(settings: Settings) -> DashboardState:
    alerts = AlertStore(settings.alert_file, settings.timezone)
    service = StateService(settings, alerts)
    state = await service.build(force=True)
    return _freeze_timestamps(state)


def _write_state(state: DashboardState) -> None:
    payload = state.model_dump_json(indent=2)
    STATE_PATH.write_text(payload + "\n", encoding="utf-8")

    # Confirm the JSON round-trips: a change to models.py that makes a Block
    # subclass lose fields on the way back through model_validate_json would
    # otherwise pass silently, since the DashboardState written above is never
    # compared against anything else.
    reloaded = DashboardState.model_validate_json(payload)
    if reloaded != state:
        raise RuntimeError(
            "frozen-state.json does not round-trip: "
            "DashboardState.model_validate_json(...) != the model that produced it"
        )


async def _render_hashes(settings: Settings, state: DashboardState) -> dict[str, str]:
    renderer = Renderer(settings)
    await renderer.start()
    try:
        hashes: dict[str, str] = {}
        for page in PAGES:
            png = await renderer.render_png(page, state)
            hashes[page] = hashlib.sha256(png).hexdigest()
        return hashes
    finally:
        await renderer.close()


async def _main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(Path(tmp))
        try:
            state = await _build_frozen_state(settings)
            _write_state(state)
            hashes = await _render_hashes(settings, state)
        finally:
            # The fixture device adapter still opens the (empty) telemetry
            # sqlite file under DATA_DIR to check for pushed samples; close it
            # before the temp directory is removed, or Windows refuses to
            # delete a file that is still open.
            close_telemetry_stores()

    HASHES_PATH.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for page in PAGES:
        print(f"{page}: {hashes[page]}")


if __name__ == "__main__":
    asyncio.run(_main())
