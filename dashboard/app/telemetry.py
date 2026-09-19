"""The telemetry the reTerminal E1002 posts every 5 minutes, stored in the
hub's one database (``DATA_DIR/deskmate.sqlite``, see ``app/db.py``).

Design notes:

* This module no longer owns a file or a connection. :class:`Database` owns
  both, so the device's inserts, the settings rows and the hub identity share
  one WAL journal and one backup file. An install from before that change
  keeps its old ``telemetry.sqlite`` on disk; ``app/legacy.py`` copies its
  rows in once and leaves the file alone.
* Timestamps are stored as fixed-width UTC ISO strings. Every row uses the
  same ``+00:00`` suffix and millisecond precision, so a string comparison is
  a time comparison and the index on ``received_at`` does range scans.
* Rows older than the retention window are deleted inside the same
  transaction as each insert. That is one indexed ``DELETE`` every 5 minutes,
  which is cheaper than owning a background job.
* The store is a thin, cheap view over the database: constructing one opens
  nothing, so :func:`get_telemetry_store` can hand out a fresh instance per
  call without a registry of its own.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Final

from app.config import Settings
from app.db import Database, get_database
from app.logging_setup import log
from app.models import DeviceSample, DeviceTelemetry

logger = logging.getLogger("app.telemetry")

_COLUMNS: Final[tuple[str, ...]] = (
    "received_at",
    "device",
    "battery_voltage",
    "battery_level",
    "temperature",
    "humidity",
    "wifi_rssi",
    "uptime_s",
    "page",
    "battery_mode",
    "usb_present",
    "charge_state",
    "wake_cause",
)

#: Origin of the POST itself: who sent it, and what hub URL they used to
#: reach it. Never part of :class:`DeviceSample` / :class:`DeviceTelemetry`
#: (the device's own reading), so they never appear in ``_SELECT_SQL`` and
#: never round-trip through GET /api/device/telemetry or /history. Stored
#: only so :func:`TelemetryStore.latest_origin` can answer the System page's
#: DEVICE IP / HUB URL rows.
_ORIGIN_COLUMNS: Final[tuple[str, ...]] = ("remote_addr", "hub_host")

_INSERT_COLUMNS: Final[tuple[str, ...]] = _COLUMNS + _ORIGIN_COLUMNS
_INSERT_SQL: Final[str] = (
    "INSERT INTO telemetry ("
    + ", ".join(_INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" * len(_INSERT_COLUMNS))
    + ")"
)
_SELECT_SQL: Final[str] = "SELECT " + ", ".join(_COLUMNS) + " FROM telemetry"


def utc_now() -> datetime:
    return datetime.now(tz=dt_timezone.utc)


def utc_iso(value: datetime) -> str:
    """Fixed-width UTC ISO string. Naive input is read as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc).isoformat(timespec="milliseconds")


def parse_utc(value: str) -> datetime:
    """Inverse of :func:`utc_iso`, tolerant of rows written by older builds."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


@dataclass(frozen=True, slots=True)
class TelemetrySummary:
    """Cheap ``COUNT/MIN/MAX`` over the whole table."""

    sample_count: int
    oldest: datetime | None
    newest: datetime | None


class TelemetryStore:
    """Append-only telemetry history with a retention window.

    Holds the shared :class:`Database`, never a connection of its own: the
    lifetime belongs to the registry in ``app/db.py``, so closing a store is
    not something a caller does.
    """

    def __init__(self, database: Database, retention_days: int) -> None:
        self._database = database
        self._retention_days = max(1, int(retention_days))

    @property
    def path(self) -> Path:
        """The database file behind this store."""
        return self._database.path

    @property
    def database(self) -> Database:
        return self._database

    @property
    def retention_days(self) -> int:
        return self._retention_days

    # -- writing ---------------------------------------------------------
    def insert(
        self,
        telemetry: DeviceTelemetry,
        *,
        received_at: datetime | None = None,
        remote_addr: str | None = None,
        hub_host: str | None = None,
    ) -> datetime:
        """Store one sample, prune what fell out of the window, return the stamp.

        ``remote_addr``/``hub_host`` describe the POST itself (who sent it,
        which hub URL they used), not the device's own reading; the caller
        (main.py) derives and caps them from the request. Never part of
        :class:`DeviceTelemetry`, so never validated or shaped by that model.
        """
        stamp = (received_at or utc_now()).astimezone(dt_timezone.utc)
        # Storage keeps milliseconds; return exactly what a later read gives back.
        stamp = stamp.replace(microsecond=(stamp.microsecond // 1000) * 1000)
        cutoff = utc_iso(stamp - timedelta(days=self._retention_days))
        row = (
            utc_iso(stamp),
            telemetry.device,
            telemetry.battery_voltage,
            telemetry.battery_level,
            telemetry.temperature,
            telemetry.humidity,
            telemetry.wifi_rssi,
            telemetry.uptime_s,
            telemetry.page,
            telemetry.battery_mode,
            telemetry.usb_present,
            telemetry.charge_state,
            telemetry.wake_cause,
            remote_addr,
            hub_host,
        )
        with self._database.writing() as connection:
            connection.execute(_INSERT_SQL, row)
            pruned = connection.execute(
                "DELETE FROM telemetry WHERE received_at < ?", (cutoff,)
            ).rowcount
        log(
            logger,
            logging.INFO,
            "telemetry stored",
            device=telemetry.device,
            temperature=telemetry.temperature,
            humidity=telemetry.humidity,
            battery_level=telemetry.battery_level,
            wifi_rssi=telemetry.wifi_rssi,
            page=telemetry.page,
            pruned=max(0, pruned),
        )
        return stamp

    # -- reading ---------------------------------------------------------
    def latest(self) -> DeviceSample | None:
        with self._database.reading() as connection:
            row = connection.execute(
                _SELECT_SQL + " ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else _to_sample(row)

    def latest_origin(self) -> tuple[str | None, str | None, datetime] | None:
        """``(remote_addr, hub_host, received_at)`` of the newest row, or
        ``None`` when the table is empty. Its own query, never folded into
        :func:`_SELECT_SQL` / :func:`_to_sample`: those feed
        :class:`DeviceSample`, and these two columns must never reach it.
        """
        with self._database.reading() as connection:
            row = connection.execute(
                "SELECT remote_addr, hub_host, received_at FROM telemetry"
                " ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return row["remote_addr"], row["hub_host"], parse_utc(str(row["received_at"]))

    def summary(self) -> TelemetrySummary:
        with self._database.reading() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n, MIN(received_at) AS oldest,"
                " MAX(received_at) AS newest FROM telemetry"
            ).fetchone()
        count = int(row["n"] or 0)
        return TelemetrySummary(
            sample_count=count,
            oldest=parse_utc(row["oldest"]) if row["oldest"] else None,
            newest=parse_utc(row["newest"]) if row["newest"] else None,
        )

    def history(self, hours: float, *, now: datetime | None = None) -> list[DeviceSample]:
        """Every sample of the last ``hours``, oldest first."""
        cutoff = utc_iso((now or utc_now()) - timedelta(hours=hours))
        with self._database.reading() as connection:
            rows = connection.execute(
                _SELECT_SQL + " WHERE received_at >= ? ORDER BY received_at ASC", (cutoff,)
            ).fetchall()
        return [_to_sample(row) for row in rows]

    def prune(self, *, now: datetime | None = None) -> int:
        """Delete rows older than the retention window. Returns the row count."""
        cutoff = utc_iso((now or utc_now()) - timedelta(days=self._retention_days))
        with self._database.writing() as connection:
            removed = connection.execute(
                "DELETE FROM telemetry WHERE received_at < ?", (cutoff,)
            ).rowcount
        return max(0, removed)


def _to_sample(row: sqlite3.Row) -> DeviceSample:
    values: dict[str, Any] = dict(row)
    values["received_at"] = parse_utc(str(values["received_at"]))
    return DeviceSample.model_validate(values)


def get_telemetry_store(settings: Settings) -> TelemetryStore:
    """The store over the process-wide database for this ``DATA_DIR``.

    The database is shared; only this thin wrapper is new per call, so the
    device adapter can ask for it on every fetch. :meth:`Database.migrate` is
    a no-op after the first call in this process, and it is what lets an
    adapter run in a test that never built a ``Hub``.
    """
    database = get_database(settings.hub_db_file)
    database.migrate()
    return TelemetryStore(database, settings.telemetry_retention_days)


__all__ = [
    "TelemetryStore",
    "TelemetrySummary",
    "get_telemetry_store",
    "parse_utc",
    "utc_iso",
    "utc_now",
]
