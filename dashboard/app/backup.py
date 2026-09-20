"""Backup and restore of the hub's one SQLite file.

The hub keeps everything in ``DATA_DIR/deskmate.sqlite`` (see ``app/db.py``),
so a backup is one file to download and a restore is one file to put back.
This module holds the parts that are pure or plain synchronous I/O - naming a
temp file, validating an uploaded one - so a test can drive every rule with
nothing but a temp directory. The routes in ``app/settings_pages.py`` own the
HTTP shape (the restore upload's own byte-counted stream and Content-Length
cap live in ``app/httputil.py``), and ``Database.backup_to`` /
``Database.replace_file`` own the lock discipline.

Why validate before swapping rather than after: the swap is destructive (the
live file is replaced, not merged), and the only honest way to refuse a file
is to refuse it while the live database is still the one in place. So the
upload lands next to the database under its own name, is opened **read-only**
through a second connection (``mode=ro``, so a malformed file cannot be
"repaired" into the data directory by the act of looking at it), and has to
answer three questions before anything is replaced:

* ``PRAGMA integrity_check`` says ``ok`` - it is a database, and not a
  truncated or corrupted one;
* ``meta.db_schema_version`` exists and is at most :data:`DB_SCHEMA_VERSION` -
  a file from a newer build would otherwise stop the hub on the next start,
  with the old file already gone;
* the ``hub`` row is there - a database with no identity would leave the hub
  unconfigured and unreachable, with no token to get back in with.

A backup carries the session secret, every secret hash and any Home Assistant
token, so it is a credential in its own right; it does **not** carry
``DATA_DIR/modules/``, which is files on disk beside the database.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.db import DB_SCHEMA_VERSION, VERSION_KEY
from app.logging_setup import log

logger = logging.getLogger("app.backup")

#: What a browser is told a backup is. SQLite's own registered media type.
BACKUP_MEDIA_TYPE = "application/vnd.sqlite3"

#: The first 16 bytes of every SQLite file. Checked before the file is opened
#: so "you uploaded the wrong file" and "this backup is damaged" are two
#: different sentences: past this header, an sqlite error means damage.
SQLITE_MAGIC = b"SQLite format 3\x00"

#: Cap on a restore upload. A hub database is telemetry plus a few rows of
#: settings; 64 MiB is far past any real one, and small enough that a hostile
#: (or mistaken) upload cannot fill the data directory before it is refused.
MAX_RESTORE_BYTES = 64 * 1024 * 1024


class RestoreRejected(Exception):
    """The uploaded file is not a backup this build can restore. The message
    is shown to the admin on the settings page, so it says which rule the
    file failed, never a path or a value out of the file."""


@dataclass(frozen=True, slots=True)
class BackupFacts:
    """What :func:`inspect_backup` learned from a file that passed.

    ``device_key_sha256`` is the one value the restore flow needs out of the
    backup before it swaps: when it differs from the live hub's, the flashed
    device stops fetching until its key is updated.
    """

    schema_version: int
    device_key_sha256: str


def backup_filename(now: datetime) -> str:
    """``deskmate-backup-<UTC YYYYMMDD-HHMMSS>.sqlite``: sorts by name, says
    which hub minute it came from, and needs no quoting in a
    ``Content-Disposition`` header."""
    return f"deskmate-backup-{now.strftime('%Y%m%d-%H%M%S')}.sqlite"


def backup_temp_path(data_dir: Path) -> Path:
    """A fresh name in ``DATA_DIR`` for ``VACUUM INTO``, which refuses to
    write a file that already exists: the random part is what makes two
    downloads in the same second, or one left behind by a killed process,
    unable to collide."""
    return data_dir / f"backup-{secrets.token_hex(8)}.sqlite-tmp"


def restore_temp_path(data_dir: Path) -> Path:
    """Where an upload lands while it is validated. In ``DATA_DIR`` rather
    than the system temp directory for two reasons: it is the volume the
    operator sized for this data, and ``os.replace`` onto the database is
    only atomic within one filesystem."""
    return data_dir / f"restore-{secrets.token_hex(8)}.sqlite-tmp"


def inspect_backup(path: Path) -> BackupFacts:
    """Open ``path`` read-only and check it is a restorable backup.

    Raises :class:`RestoreRejected` with a one-line reason for anything that
    is not: a file that is not SQLite at all, one that fails
    ``PRAGMA integrity_check``, one without a readable
    ``meta.db_schema_version``, one written by a newer build, or one with no
    ``hub`` row. Synchronous I/O: call it through ``run_in_threadpool``.
    """
    if _head(path) != SQLITE_MAGIC:
        raise RestoreRejected("the uploaded file is not an SQLite database")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise RestoreRejected("the uploaded file could not be opened as a database") from exc
    try:
        # Damage shows up either way round: a verdict that is not "ok", or an
        # exception when the corruption is in a page sqlite has to parse
        # before it can even report on it. Both mean the same thing here.
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            verdict = None if row is None else str(row[0])
        except sqlite3.DatabaseError:
            verdict = None
        if verdict != "ok":
            raise RestoreRejected(
                "the uploaded file failed SQLite's integrity check; it is damaged"
            )
        version = _read_backup_version(connection)
        if version is None:
            raise RestoreRejected(
                "the uploaded file carries no database schema version; it is not a "
                "deskmate backup"
            )
        if version > DB_SCHEMA_VERSION:
            raise RestoreRejected(
                f"the uploaded backup is at database schema {version} and this build "
                f"understands {DB_SCHEMA_VERSION}; upgrade the image first"
            )
        device_key_sha256 = _read_backup_device_key(connection)
        if device_key_sha256 is None:
            raise RestoreRejected(
                "the uploaded backup has no hub row; restoring it would leave this "
                "hub unconfigured"
            )
    finally:
        connection.close()
    log(logger, logging.INFO, "backup accepted", schema=version)
    return BackupFacts(schema_version=version, device_key_sha256=device_key_sha256)


def _head(path: Path) -> bytes:
    """The file's first 16 bytes, or ``b""`` when it cannot be read at all."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC))
    except OSError:
        return b""


def _read_backup_version(connection: sqlite3.Connection) -> int | None:
    """The backup's ``meta.db_schema_version`` as an int, or ``None`` when
    the table, the row or the value is missing or unreadable. An unparseable
    value is not "None" but "newer than anything this build knows", the same
    reading ``db.py:_read_version`` takes, so it is refused rather than
    waved through."""
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (VERSION_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    try:
        return int(str(row[0]))
    except ValueError:
        return DB_SCHEMA_VERSION + 1


def _read_backup_device_key(connection: sqlite3.Connection) -> str | None:
    """The backup's ``hub.device_key_sha256``, or ``None`` when there is no
    ``hub`` table or no row in it."""
    try:
        row = connection.execute("SELECT device_key_sha256 FROM hub WHERE id = 1").fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None or not str(row[0]):
        return None
    return str(row[0])


__all__ = [
    "BACKUP_MEDIA_TYPE",
    "MAX_RESTORE_BYTES",
    "SQLITE_MAGIC",
    "BackupFacts",
    "RestoreRejected",
    "backup_filename",
    "backup_temp_path",
    "inspect_backup",
    "restore_temp_path",
]
