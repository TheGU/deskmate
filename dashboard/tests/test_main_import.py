"""``import app.main`` must not build a hub.

``app/main.py`` used to end with a module-level ``app = create_app()``,
which ran on every import of the module - including under pytest, before
any test asked for a hub at all - and built a real :class:`Hub` against
``Env()``'s own default ``data_dir`` (the repository's ``data/`` directory,
``app/config.py:REPO_ROOT / "data"``), creating ``deskmate.sqlite`` there
regardless of the process's own working directory (``REPO_ROOT`` is derived
from ``__file__``, not from cwd).

Proving that requires ``__file__`` to resolve inside a throwaway directory
rather than this checkout, so the app package is copied into a fresh
``tmp_path`` first; a bare ``import app.main`` is then run there in a
subprocess with a temporary cwd, and the only thing checked is that the
copy's own ``data/`` directory - where ``Env()``'s default would land -
never appears.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

#: dashboard/tests/test_main_import.py -> dashboard/
DASHBOARD_DIR = Path(__file__).resolve().parents[1]


def test_import_creates_no_files(tmp_path: Path) -> None:
    # A private copy of the app package, so app/config.py's
    # ``Path(__file__).resolve().parents[2]`` (REPO_ROOT) lands inside
    # tmp_path instead of this actual checkout - otherwise a regression
    # would silently write into the real repository's data/ directory
    # rather than somewhere this test can see.
    project_root = tmp_path / "project"
    dashboard_dir = project_root / "dashboard"
    shutil.copytree(
        DASHBOARD_DIR / "app",
        dashboard_dir / "app",
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    env = dict(os.environ)
    # DATA_DIR and a developer .env must never leak into this probe: the
    # point is Env()'s bare default, not whatever this machine happens to
    # have configured. PYTHONDONTWRITEBYTECODE keeps an ordinary .pyc cache
    # out of the "did anything appear" check below.
    env.pop("DATA_DIR", None)
    env["PYTHONPATH"] = str(dashboard_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=dashboard_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    # Env()'s default data_dir, where the old module-level create_app() call
    # used to write deskmate.sqlite.
    assert not (project_root / "data").exists()
    # Nothing else appeared in the fresh tree either, only the app copy
    # placed there before the subprocess ran.
    assert list(project_root.iterdir()) == [dashboard_dir]
    assert list(dashboard_dir.iterdir()) == [dashboard_dir / "app"]
