"""Current-alert store with the priority rules from docs/ARCHITECTURE.md.

``critical > doorbell > important > normal``. A lower priority never replaces a
higher priority alert that is still inside its duration window. The alert is
kept in memory and mirrored to ``data/alert.json`` so a restart does not lose
whatever the device is currently showing.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app.logging_setup import log
from app.models import Alert, AlertRequest
from app.timeutil import now_local, to_local

logger = logging.getLogger("app.alerts")


class AlertStore:
    """Holds at most one alert. Simple on purpose; a queue can come later."""

    def __init__(self, path: Path, timezone_name: str) -> None:
        self._path = path
        self._timezone = timezone_name
        self._alert: Alert | None = None
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        if not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                self._alert = Alert.model_validate(json.load(handle))
        except Exception as exc:  # noqa: BLE001 - a bad file must not block startup
            log(logger, logging.WARNING, "unreadable alert file", path=str(self._path), error=str(exc))
            self._alert = None

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._alert is None:
                self._path.unlink(missing_ok=True)
                return
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._path.parent, delete=False, suffix=".tmp"
            )
            with handle:
                handle.write(self._alert.model_dump_json(indent=2))
            os.replace(handle.name, self._path)
        except OSError as exc:
            log(logger, logging.WARNING, "cannot persist alert", path=str(self._path), error=str(exc))

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
