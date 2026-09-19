"""The one SQLite file the hub owns: identity, settings, pushed datasets and
telemetry, all in ``DATA_DIR/deskmate.sqlite``.

Why one file rather than the four the hub used to keep (``hub.json``,
``alert.json``, the pushed JSON files, ``telemetry.sqlite``): a backup is then
one file to copy and a restore is one file to put back, and a settings page
that has to write identity, a source selector and a pushed dataset can do it
without inventing a second consistency story per file.

Design notes, the same ones ``telemetry.py`` has always followed:

* stdlib ``sqlite3`` only, WAL journal, so a reader (a page render) never
  blocks the writer (the device posting telemetry, or a settings save).
* ``check_same_thread=False`` plus one ``threading.Lock``: FastAPI runs the
  synchronous handlers in its thread pool, and the lock is what makes the
  single shared connection safe across them. Take it through
  :meth:`Database.reading` / :meth:`Database.writing`, never by hand.
* Every write commits before the lock is released. There is no background
  flush and nothing to lose on a hard stop.
* Timestamps are fixed-width UTC ISO strings with millisecond precision, so a
  string comparison is a time comparison and an index does range scans.

The connection is owned by a process-wide registry keyed by the resolved path
(:func:`get_database`), the same pattern ``telemetry.py:get_telemetry_store``
used before it. ``Hub`` holds a reference; it does not own the lifetime, so
two ``create_app()`` calls over one ``DATA_DIR`` share one connection instead
of racing each other through two. :func:`close_databases` is the hook tests
and a shutdown use; :meth:`Database.close` plus :meth:`Database.reopen` are
what the restore flow needs to swap the file underneath a running process.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Final

from app.logging_setup import log

logger = logging.getLogger("app.db")

#: Bumped whenever a released schema changes in a way a previous build cannot
#: read. Unrelated to ``hub_config.HUB_CONFIG_SCHEMA``, which numbered the old
#: ``hub.json`` file and now only describes what the legacy import reads.
DB_SCHEMA_VERSION: Final[int] = 1

VERSION_KEY: Final[str] = "db_schema_version"
LEGACY_IMPORTED_KEY: Final[str] = "legacy_imported_at"

#: Created first and on its own: the version check has to be able to read
#: ``meta`` before the rest of the schema is touched, so a database written by
#: a newer build is refused without this process altering it at all.
_META_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: ``hub`` holds exactly one row (``id`` is pinned to 1 by the CHECK): the
#: hub's identity, replacing ``data/hub.json``. ``settings`` holds one JSON
#: document per section, ``datasets`` one per pushed dataset, ``telemetry``
#: the device history with the columns ``telemetry.py`` has always written,
#: including the four that were added by migration after it first shipped and
#: the two origin columns.
_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS hub (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    name              TEXT NOT NULL,
    base_url          TEXT NOT NULL,
    token_sha256      TEXT NOT NULL,
    device_key_sha256 TEXT NOT NULL,
    session_secret    TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    section    TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    name         TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
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
    charge_state    TEXT,
    wake_cause      TEXT,
    remote_addr     TEXT,
    hub_host        TEXT
);
CREATE INDEX IF NOT EXISTS telemetry_received_at ON telemetry (received_at);
"""

#: Telemetry columns added after that table first shipped. ``CREATE TABLE IF
#: NOT EXISTS`` above only builds a brand-new table, so a ``telemetry`` table
#: that already exists without them (an old ``telemetry.sqlite`` opened
#: directly, or one this build attached and copied from) gets them added here.
_TELEMETRY_MIGRATION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("battery_mode", "INTEGER"),
    ("usb_present", "INTEGER"),
    ("charge_state", "TEXT"),
    ("wake_cause", "TEXT"),
    ("remote_addr", "TEXT"),
    ("hub_host", "TEXT"),
)


def utc_now_iso() -> str:
    """The stamp format every ``*_at`` column in this file uses."""
    return datetime.now(tz=dt_timezone.utc).isoformat(timespec="milliseconds")


class Database:
    """One SQLite connection, one lock, and the schema it carries.

    Callers reach the connection through :meth:`reading` and :meth:`writing`
    so the lock is never forgotten, and so a write always commits. Both are
    plain synchronous I/O: call them from ``run_in_threadpool`` when the
    caller is an async route.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._key = _registry_key(self.path)
        self._lock = threading.Lock()
        self._migrated = False
        self._connection = _connect(self.path)
        log(logger, logging.INFO, "database opened", path=str(self.path))

    # -- connection ------------------------------------------------------
    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """The connection, under the lock, for statements that only read."""
        with self._lock:
            yield self._connection

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        """The connection, under the lock, committed on the way out.

        A failure rolls back before the exception leaves, so a half-written
        multi-statement change never survives into the next reader.
        """
        with self._lock:
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            self._connection.commit()

    # -- schema ----------------------------------------------------------
    def migrate(self) -> int:
        """Create whatever is missing and return the schema version.

        Idempotent and cheap after the first call in this process, so a hot
        path (the device adapter asking for the telemetry store on every
        fetch) can call it without thinking about it.

        Raises :class:`RuntimeError` when the file was written by a newer
        build: a database this code does not understand must stop the hub,
        not be silently downgraded by a ``CREATE TABLE IF NOT EXISTS`` sweep
        that leaves half its columns unread.
        """
        if self._migrated:
            return DB_SCHEMA_VERSION
        with self.writing() as connection:
            connection.executescript(_META_SCHEMA)
            found = _read_version(connection)
            if found is not None and found > DB_SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} was written by a newer dashboard-hub "
                    f"(database schema {found}, this build understands "
                    f"{DB_SCHEMA_VERSION}); restore a backup from this build or "
                    "upgrade the image"
                )
            connection.executescript(_SCHEMA)
            _migrate_telemetry_columns(connection)
            if found != DB_SCHEMA_VERSION:
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (VERSION_KEY, str(DB_SCHEMA_VERSION)),
                )
                log(
                    logger,
                    logging.INFO,
                    "database migrated",
                    path=str(self.path),
                    was=found,
                    now=DB_SCHEMA_VERSION,
                )
        self._migrated = True
        return DB_SCHEMA_VERSION

    @property
    def schema_version(self) -> int | None:
        """The version recorded in ``meta``, or ``None`` before the first
        :meth:`migrate` has ever run against this file."""
        with self.reading() as connection:
            try:
                return _read_version(connection)
            except sqlite3.OperationalError:
                return None

    # -- meta ------------------------------------------------------------
    def meta_get(self, key: str) -> str | None:
        with self.reading() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def meta_set(self, key: str, value: str) -> None:
        with self.writing() as connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- lifetime --------------------------------------------------------
    def close(self) -> None:
        """Close the connection and drop this database out of the registry.

        Leaving the registry is the point: the next :func:`get_database` for
        this path opens a fresh connection rather than handing back a closed
        one. :meth:`reopen` is the other half, for a restore that replaces
        the file underneath the process.
        """
        _forget(self._key, self)
        with self._lock:
            self._connection.close()
            self._migrated = False

    def reopen(self) -> None:
        """Open the file at this path again and re-register the database.

        Used by the restore flow: close, replace the file, reopen, migrate.
        Every holder of this object (``Hub``, the stores built from it) keeps
        working, because the object identity never changed.
        """
        with self._lock:
            self._connection = _connect(self.path)
            self._migrated = False
        with _REGISTRY_LOCK:
            _DATABASES[self._key] = self
        log(logger, logging.INFO, "database reopened", path=str(self.path))


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.commit()
    return connection


def _read_version(connection: sqlite3.Connection) -> int | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (VERSION_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return int(str(row["value"]))
    except ValueError:
        # An unreadable version is treated as "newer than anything": refusing
        # is the only safe reading of a value this build cannot parse.
        return DB_SCHEMA_VERSION + 1


def _migrate_telemetry_columns(connection: sqlite3.Connection) -> None:
    existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(telemetry)")}
    for name, column_type in _TELEMETRY_MIGRATION_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE telemetry ADD COLUMN {name} {column_type}")
            log(logger, logging.INFO, "telemetry column added", column=name)


# ---------------------------------------------------------------------------
# Process-wide registry, keyed by resolved path
# ---------------------------------------------------------------------------
_DATABASES: dict[str, Database] = {}
_REGISTRY_LOCK = threading.Lock()


def _registry_key(path: Path) -> str:
    """The resolved path, so ``data/deskmate.sqlite`` and an absolute spelling
    of the same file are one entry rather than two connections."""
    return str(Path(path).resolve())


def get_database(path: Path) -> Database:
    """The process-wide database for ``path``, opened on first use.

    Not migrated: the caller decides when the schema runs, because a failed
    version check has to be able to stop startup rather than surprise an
    adapter mid-fetch.
    """
    key = _registry_key(path)
    with _REGISTRY_LOCK:
        database = _DATABASES.get(key)
        if database is None:
            database = Database(Path(path))
            _DATABASES[key] = database
        return database


def close_databases() -> None:
    """Close every registered database (shutdown, and tests on a temp dir)."""
    with _REGISTRY_LOCK:
        databases = list(_DATABASES.values())
        _DATABASES.clear()
    for database in databases:
        database.close()


def _forget(key: str, database: Database) -> None:
    with _REGISTRY_LOCK:
        if _DATABASES.get(key) is database:
            del _DATABASES[key]


__all__ = [
    "DB_SCHEMA_VERSION",
    "Database",
    "LEGACY_IMPORTED_KEY",
    "VERSION_KEY",
    "close_databases",
    "get_database",
    "utc_now_iso",
]
