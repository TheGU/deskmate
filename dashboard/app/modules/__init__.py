"""Where a module lives.

Phase 1 (this package, today) only uses this tree to hold each section's
settings model at the path phase 2 will expect: ``app/modules/<id>/
settings.py``. The module system itself -- the ``Module``, ``DatasetSpec``
and ``PageSpec`` dataclasses, and the registry that discovers built-in
modules, entry points and ``DATA_DIR/modules/`` packages -- lands in phase 2
(work packages 2.1a and 2.1b of docs/plan/2026-09-19-settings-modules-
provisioning.md). Nothing here is importable yet beyond the section
subpackages.
"""

from __future__ import annotations
