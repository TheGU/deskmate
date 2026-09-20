"""Read and write one row of the ``datasets`` table (``app/db.py``).

Every pushed dataset (``tasks``, ``ai_usage``, ``brief``) is one row: a JSON
payload plus when it arrived. The three push routes in ``app/main.py`` and
the matching ``Push*Adapter`` classes in ``app/adapters/`` are the main
callers; ``app/alerts.py`` also reads and writes through this module for its
``alert`` row (``alert`` is not a pushed dataset a module can claim, so it
is never listed alongside them), and reads the payload back into a specific
model rather than a bare dict; its delete-on-clear path has no equivalent
here and still runs its own ``DELETE``. ``app/legacy.py`` writes rows
directly with its own ``INSERT`` (it is a one-time import that must never
overwrite an existing row, which :func:`write_dataset`'s upsert would do),
but writes the same two columns this module reads, so a legacy-imported row
loads through :func:`read_dataset` exactly as a pushed one does.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from typing import Any

from app.db import Database, utc_now_iso


def read_dataset(db: Database, name: str) -> tuple[dict[str, Any], datetime] | None:
    """The stored payload and its ``received_at``, or ``None`` when nothing
    has ever been pushed or imported for ``name``.

    ``received_at`` is parsed as an aware UTC datetime. Every writer in this
    codebase stores a UTC ISO string with an explicit offset, but a value
    with none (a hand-edited row) is read as UTC rather than raising, the
    same fallback ``app/legacy.py:_received_at`` applies to a pushed file
    with no ``received_at`` key of its own.
    """
    with db.reading() as connection:
        row = connection.execute(
            "SELECT payload_json, received_at FROM datasets WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload_json"]))
    received_at = datetime.fromisoformat(str(row["received_at"]))
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=dt_timezone.utc)
    return payload, received_at


def write_dataset(
    db: Database, name: str, payload: dict[str, Any], received_at: datetime | None = None
) -> None:
    """Upsert ``name``'s row: the payload as JSON, ``received_at`` as the
    same fixed-width UTC ISO string every other ``*_at`` column in
    ``app/db.py`` uses (``received_at`` defaults to now, in UTC, when the
    caller does not already have a moment to record - a push route always
    does, so it always passes one).
    """
    stamp = (
        utc_now_iso()
        if received_at is None
        else received_at.astimezone(dt_timezone.utc).isoformat(timespec="milliseconds")
    )
    with db.writing() as connection:
        connection.execute(
            "INSERT INTO datasets (name, payload_json, received_at) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET"
            " payload_json = excluded.payload_json,"
            " received_at = excluded.received_at",
            (name, json.dumps(payload), stamp),
        )


__all__ = ["read_dataset", "write_dataset"]
