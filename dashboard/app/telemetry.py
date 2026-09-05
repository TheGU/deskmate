"""SQLite store for the telemetry the reTerminal E1002 posts every 5 minutes.

Design notes:

* stdlib ``sqlite3`` only. One file, one table, one index on ``received_at``.
* WAL journal, so a reader (page render) never blocks the writer (the device).
* Timestamps are stored as fixed-width UTC ISO strings. Every row uses the
  same ``+00:00`` suffix and millisecond precision, so a string comparison is
  a time comparison and the index on ``received_at`` does range scans.
* Rows older than ``TELEMETRY_RETENTION_DAYS`` are deleted inside the same
  transaction as each insert. That is one indexed ``DELETE`` every 5 minutes,
  which is cheaper than owning a background job.
* One connection guarded by a ``threading.Lock``: FastAPI runs the telemetry
  handlers in the thread pool, so the lock is what makes this thread safe.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Final

from app.config import Settings
from app.logging_setup import log
from app.models import DeviceSample, DeviceTelemetry

logger = logging.getLogger("app.telemetry")

SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS telemetry (
    received_at     TEXT NOT NULL,
    device          TEXT NOT NULL,
    battery_voltage REAL,
    battery_level   REAL,
    temperature     REAL,
    humidity        REAL,
    wifi_rssi       REAL,
    uptime_s        REAL,
    page            TEXT,
    battery_mode    INTEGER,
    usb_present     INTEGER,
    charge_state    TEXT
);
CREATE INDEX IF NOT EXISTS telemetry_received_at ON telemetry (received_at);
"""

#: Columns added after the table first shipped. ``open()`` adds whichever of
#: these a pre-existing database file is still missing, so an old install can
#: be started against a new build without a manual migration step.
_MIGRATION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("battery_mode", "INTEGER"),
    ("usb_present", "INTEGER"),
    ("charge_state", "TEXT"),
)

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
)

_INSERT_SQL: Final[str] = (
    "INSERT INTO telemetry (" + ", ".join(_COLUMNS) + ") VALUES (" + ", ".join("?" * len(_COLUMNS)) + ")"
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
    """Append-only telemetry history with a retention window."""

    def __init__(self, path: Path, retention_days: int) -> None:
        self._path = path
        self._retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(SCHEMA)
            self._migrate_locked()
            self._connection.commit()
        log(
            logger,
            logging.INFO,
            "telemetry store opened",
            path=str(path),
            retention_days=self._retention_days,
        )

    def _migrate_locked(self) -> None:
        """Add any column from ``_MIGRATION_COLUMNS`` a pre-existing file lacks.

        Caller holds ``self._lock``. ``CREATE TABLE IF NOT EXISTS`` only
        matters for a brand-new file; a database opened from an older build
        already has the ``telemetry`` table without these columns, so they
        have to be added with ``ALTER TABLE`` instead.
        """
        existing = {
            str(row["name"]) for row in self._connection.execute("PRAGMA table_info(telemetry)")
        }
        for name, column_type in _MIGRATION_COLUMNS:
            if name not in existing:
                self._connection.execute(
                    f"ALTER TABLE telemetry ADD COLUMN {name} {column_type}"
                )
                log(logger, logging.INFO, "telemetry column added", column=name)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def retention_days(self) -> int:
        return self._retention_days

    # -- writing ---------------------------------------------------------
    def insert(
        self, telemetry: DeviceTelemetry, *, received_at: datetime | None = None
    ) -> datetime:
        """Store one sample, prune what fell out of the window, return the stamp."""
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
        )
        with self._lock:
            self._connection.execute(_INSERT_SQL, row)
            pruned = self._connection.execute(
                "DELETE FROM telemetry WHERE received_at < ?", (cutoff,)
            ).rowcount
            self._connection.commit()
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
        with self._lock:
            row = self._connection.execute(
                _SELECT_SQL + " ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else _to_sample(row)

    def summary(self) -> TelemetrySummary:
        with self._lock:
            row = self._connection.execute(
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
        with self._lock:
            rows = self._connection.execute(
                _SELECT_SQL + " WHERE received_at >= ? ORDER BY received_at ASC", (cutoff,)
            ).fetchall()
        return [_to_sample(row) for row in rows]

    def prune(self, *, now: datetime | None = None) -> int:
        """Delete rows older than the retention window. Returns the row count."""
        cutoff = utc_iso((now or utc_now()) - timedelta(days=self._retention_days))
        with self._lock:
            removed = self._connection.execute(
                "DELETE FROM telemetry WHERE received_at < ?", (cutoff,)
            ).rowcount
            self._connection.commit()
        return max(0, removed)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _to_sample(row: sqlite3.Row) -> DeviceSample:
    values: dict[str, Any] = dict(row)
    values["received_at"] = parse_utc(str(values["received_at"]))
    return DeviceSample.model_validate(values)


# ---------------------------------------------------------------------------
# Process-wide stores, keyed by database file
# ---------------------------------------------------------------------------
_STORES: dict[tuple[str, int], TelemetryStore] = {}
_STORES_LOCK = threading.Lock()


def get_telemetry_store(settings: Settings) -> TelemetryStore:
    """One store per database file, shared by the HTTP handlers and the adapter."""
    key = (str(settings.telemetry_db_file), settings.telemetry_retention_days)
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = TelemetryStore(Path(key[0]), key[1])
            _STORES[key] = store
        return store


def close_telemetry_stores() -> None:
    """Close every cached store (shutdown, and tests that use a temp directory)."""
    with _STORES_LOCK:
        for store in _STORES.values():
            store.close()
        _STORES.clear()


__all__ = [
    "TelemetryStore",
    "TelemetrySummary",
    "close_telemetry_stores",
    "get_telemetry_store",
    "parse_utc",
    "utc_iso",
    "utc_now",
]
