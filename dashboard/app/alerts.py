"""Current-alert store with the priority rules from docs/ARCHITECTURE.md.

``critical > doorbell > important > normal``. A lower priority never replaces a
higher priority alert that is still inside its duration window. The alert is
kept in memory and mirrored to the ``alert`` row of the ``datasets`` table
(``app/db.py``) so a restart does not lose whatever the device is currently
showing. It used to be ``data/alert.json``; an install from before the
database has that file imported once by ``app/legacy.py``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from app.db import Database, utc_now_iso
from app.logging_setup import log
from app.models import Alert, AlertRequest
from app.timeutil import now_local, to_local

logger = logging.getLogger("app.alerts")

#: The ``datasets`` row this store owns. ``alert`` is reserved: it is never a
#: pushed dataset a module can claim.
DATASET_NAME = "alert"


class AlertStore:
    """Holds at most one alert. Simple on purpose; a queue can come later."""

    def __init__(self, db: Database, timezone_name: str) -> None:
        self._db = db
        self._timezone = timezone_name
        self._alert: Alert | None = None
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        """Re-read the stored alert. Called at construction and by
        ``Hub.reload()``, which must never reset what the panel is showing.
        """
        try:
            with self._db.reading() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM datasets WHERE name = ?", (DATASET_NAME,)
                ).fetchone()
        except sqlite3.Error as exc:
            log(logger, logging.WARNING, "cannot read stored alert", error=str(exc))
            self._alert = None
            return
        if row is None:
            self._alert = None
            return
        try:
            self._alert = Alert.model_validate(json.loads(str(row["payload_json"])))
        except Exception as exc:  # noqa: BLE001 - a bad row must not block startup
            log(logger, logging.WARNING, "unreadable stored alert", error=str(exc))
            self._alert = None

    def _persist(self) -> None:
        try:
            with self._db.writing() as connection:
                if self._alert is None:
                    connection.execute("DELETE FROM datasets WHERE name = ?", (DATASET_NAME,))
                    return
                connection.execute(
                    "INSERT INTO datasets (name, payload_json, received_at)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(name) DO UPDATE SET"
                    " payload_json = excluded.payload_json,"
                    " received_at = excluded.received_at",
                    (DATASET_NAME, self._alert.model_dump_json(), utc_now_iso()),
                )
        except sqlite3.Error as exc:
            log(logger, logging.WARNING, "cannot persist alert", error=str(exc))

    # -- api -------------------------------------------------------------
    @property
    def current(self) -> Alert | None:
        return self._alert

    def is_active(self, alert: Alert, at: datetime | None = None) -> bool:
        moment = at or now_local(self._timezone)
        created = to_local(alert.created_at, self._timezone)
        return moment < created + timedelta(seconds=alert.duration_seconds)

    def set(self, request: AlertRequest) -> tuple[Alert, bool]:
        """Store ``request``. Returns (effective alert, accepted)."""
        candidate = Alert(
            title=request.title,
            message=request.message,
            priority=request.priority,
            created_at=now_local(self._timezone),
            duration_seconds=request.duration_seconds,
            beep=request.beep,
            source=request.source,
        )
        existing = self._alert
        if (
            existing is not None
            and self.is_active(existing)
            and candidate.rank < existing.rank
        ):
            log(
                logger,
                logging.INFO,
                "alert rejected, lower priority",
                incoming=candidate.priority.value,
                active=existing.priority.value,
            )
            return existing, False
        self._alert = candidate
        self._persist()
        log(
            logger,
            logging.INFO,
            "alert set",
            priority=candidate.priority.value,
            title=candidate.title,
            duration=candidate.duration_seconds,
        )
        return candidate, True

    def clear(self) -> bool:
        had = self._alert is not None
        self._alert = None
        self._persist()
        if had:
            log(logger, logging.INFO, "alert cleared")
        return had
