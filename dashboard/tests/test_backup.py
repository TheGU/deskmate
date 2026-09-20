"""Backup, restore and rotate: the three admin actions that touch the hub's
one database as a file rather than as rows.

Every test here builds its own app over its own ``DATA_DIR``: a restore
replaces the database and a rotate replaces both secrets, so none of this can
run against the shared session ``client`` without wrecking whatever runs next.

The HTTP tests deliberately use ``TestClient(app)`` **without** the ``with``
block, so the lifespan never runs and no Chromium starts: none of these
routes renders a PNG, and the Jinja environment the HTML ones use is built in
``Renderer.__init__``. The one test that does render
(``test_restore_while_a_render_is_in_flight``) borrows the session renderer
and the session event loop instead, the same way the rest of the suite does.
"""

from __future__ import annotations

import asyncio
import io
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.backup import (
    MAX_RESTORE_BYTES,
    RestoreRejected,
    backup_filename,
    backup_temp_path,
    inspect_backup,
)
from app.config import REPO_ROOT, Env
from app.db import DB_SCHEMA_VERSION, Database, get_database
from app.hub_config import COOKIE_NAME, ClaimedSecrets, claim_hub, session_role, write_hub_config
from app.main import _stream_upload_to, create_app
from app.modules.ai_usage.settings import AIUsageSettings
from app.modules.brief.settings import BriefSettings
from app.modules.calendar.settings import CalendarSettings
from app.modules.device.settings import DeviceSettings
from app.modules.home.settings import HomeSettings
from app.modules.tasks.settings import TasksSettings
from app.modules.weather.settings import WeatherSettings
from app.renderer.render import Renderer
from app.settings import HubSettings
from tests.conftest import run

FIXTURES_DIR = REPO_ROOT / "fixtures"

BACKUP_NAME = re.compile(r'attachment; filename="deskmate-backup-\d{8}-\d{6}\.sqlite"')


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def hub_env(data_dir: Path) -> Env:
    return Env(
        _env_file=None,
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
    )


def fixture_hub_settings() -> HubSettings:
    """Seeded into a fresh hub's store (see ``create_app``'s docstring): every
    source ``fixture``, the same story ``tests/conftest.py``'s session
    fixture tells for the shared client."""
    return HubSettings(
        tasks=TasksSettings(source="fixture"),
        calendar=CalendarSettings(source="fixture"),
        weather=WeatherSettings(source="fixture"),
        ai_usage=AIUsageSettings(source="fixture"),
        brief=BriefSettings(source="fixture"),
        home=HomeSettings(source="fixture"),
        device=DeviceSettings(source="fixture"),
    )


class ClaimedHub:
    """One claimed hub: its app, a client, and the two plaintext secrets."""

    def __init__(self, data_dir: Path, name: str = "deskmate") -> None:
        self.env = hub_env(data_dir)
        self.app = create_app(self.env, fixture_hub_settings())
        self.hub = self.app.state.hub
        self.client = TestClient(self.app)
        self.secrets: ClaimedSecrets = asyncio.run(
            self.hub.identity.claim(name=name, base_url="http://dashboard-hub.lan:8080")
        )

    @property
    def token(self) -> str:
        return self.secrets.token

    @property
    def device_key(self) -> str:
        return self.secrets.device_key

    def admin_cookie(self) -> str:
        response = self.client.post(
            "/login", data={"key": self.token, "next": "/settings"}, follow_redirects=False
        )
        cookie = response.cookies.get(COOKIE_NAME)
        assert cookie
        return str(cookie)

    def reader_cookie(self) -> str:
        response = self.client.post(
            "/login", data={"key": self.device_key, "next": "/preview"}, follow_redirects=False
        )
        cookie = response.cookies.get(COOKIE_NAME)
        assert cookie
        return str(cookie)


@pytest.fixture()
def hub(tmp_path: Path) -> Iterator[ClaimedHub]:
    """A claimed hub on its own DATA_DIR, closed out of the registry after."""
    instance = ClaimedHub(tmp_path / "live")
    yield instance
    instance.hub.db.close()


def other_backup(data_dir: Path, name: str = "restored") -> tuple[Path, ClaimedSecrets]:
    """A backup file taken from a second, independent hub: the only honest
    way to prove a restore swapped anything is to restore credentials this
    process never wrote into the live database."""
    database = Database(data_dir / "deskmate.sqlite")
    try:
        database.migrate()
        config, token, device_key = claim_hub(
            existing=None, name=name, base_url="http://other.lan:8080"
        )
        write_hub_config(database, config)
        target = backup_temp_path(data_dir)
        database.backup_to(target)
    finally:
        database.close()
    return target, ClaimedSecrets(token=token, device_key=device_key)


def upload(path: Path, *, confirm: bool = True) -> dict[str, object]:
    """The multipart body of one restore submission."""
    files = {"file": (path.name, path.read_bytes(), "application/vnd.sqlite3")}
    data = {"confirm": "on"} if confirm else {}
    return {"files": files, "data": data}


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
def test_backup_downloads_an_sqlite_file_carrying_the_hub_row(
    hub: ClaimedHub, tmp_path: Path
) -> None:
    response = hub.client.post("/settings/backup", headers=auth(hub.token))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.sqlite3"
    assert response.headers["cache-control"] == "no-store"
    assert BACKUP_NAME.fullmatch(response.headers["content-disposition"])

    downloaded = tmp_path / "downloaded.sqlite"
    downloaded.write_bytes(response.content)
    facts = inspect_backup(downloaded)
    assert facts.schema_version == DB_SCHEMA_VERSION
    config = hub.hub.identity.config
    assert config is not None
    assert facts.device_key_sha256 == config.device_key_sha256
    with closing(sqlite3.connect(str(downloaded))) as connection:
        row = connection.execute("SELECT name, token_sha256 FROM hub WHERE id = 1").fetchone()
    assert row[0] == "deskmate"
    assert row[1] == config.token_sha256


def test_backup_leaves_no_temp_file_behind(hub: ClaimedHub) -> None:
    """The VACUUM INTO target is deleted by the response's background task,
    so DATA_DIR holds nothing but the live database and its sidecars."""
    assert hub.client.post("/settings/backup", headers=auth(hub.token)).status_code == 200
    leftovers = sorted(p.name for p in hub.env.data_dir.glob("*.sqlite-tmp"))
    assert leftovers == []


def test_backup_needs_an_admin_credential(hub: ClaimedHub) -> None:
    """The device key is a reader credential and never an admin one, as a
    bearer or through the cookie it can mint: both are refused by
    require_admin with a plain 401, the same shape POST /settings/general
    has had since package 1.3."""
    assert hub.client.post("/settings/backup").status_code == 401
    assert hub.client.post("/settings/backup", headers=auth(hub.device_key)).status_code == 401
    cookie_only = TestClient(hub.app, cookies={COOKIE_NAME: hub.reader_cookie()})
    assert cookie_only.post("/settings/backup").status_code == 401


def test_the_backup_filename_is_a_utc_stamp() -> None:
    stamp = datetime(2026, 9, 19, 4, 5, 6, tzinfo=dt_timezone.utc)
    assert backup_filename(stamp) == "deskmate-backup-20260919-040506.sqlite"


# ---------------------------------------------------------------------------
# restore: the refusals
# ---------------------------------------------------------------------------
def assert_untouched(hub: ClaimedHub, response_status: int, response_text: str) -> None:
    """A refused restore is the settings page again, with the reason on it,
    and the live hub still verifying the credentials it had."""
    assert response_status == 422
    assert "Settings" in response_text
    assert hub.hub.identity.verify_token(hub.token) is True
    assert hub.hub.identity.verify_device_key(hub.device_key) is True
    assert sorted(p.name for p in hub.env.data_dir.glob("*.sqlite-tmp")) == []


def test_restore_refuses_a_file_that_is_not_sqlite(hub: ClaimedHub, tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_bytes(b"this is not a database, it is a shopping list\n" * 40)

    response = hub.client.post(
        "/settings/restore", headers=auth(hub.token), **upload(junk)
    )

    assert "not an SQLite database" in response.text
    assert_untouched(hub, response.status_code, response.text)


def test_restore_refuses_a_newer_schema_version(hub: ClaimedHub, tmp_path: Path) -> None:
    backup, _ = other_backup(tmp_path / "newer")
    with closing(sqlite3.connect(str(backup))) as connection:
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'db_schema_version'",
            (str(DB_SCHEMA_VERSION + 1),),
        )
        connection.commit()

    response = hub.client.post(
        "/settings/restore", headers=auth(hub.token), **upload(backup)
    )

    assert "upgrade the image" in response.text
    assert_untouched(hub, response.status_code, response.text)


def test_restore_refuses_a_backup_without_a_hub_row(hub: ClaimedHub, tmp_path: Path) -> None:
    data_dir = tmp_path / "rowless"
    database = Database(data_dir / "deskmate.sqlite")
    database.migrate()
    backup = backup_temp_path(data_dir)
    database.backup_to(backup)
    database.close()

    response = hub.client.post(
        "/settings/restore", headers=auth(hub.token), **upload(backup)
    )

    assert "no hub row" in response.text
    assert_untouched(hub, response.status_code, response.text)


def test_restore_refuses_a_damaged_file(hub: ClaimedHub, tmp_path: Path) -> None:
    """A real backup with its payload chewed up: the header still says
    SQLite, so this is the "damaged" answer rather than the "wrong file"
    one, whether integrity_check reports it or raises."""
    backup, _ = other_backup(tmp_path / "damaged")
    _fill_with_telemetry(backup)
    raw = backup.read_bytes()
    # One page short of what the header says it holds: deterministic damage,
    # and exactly what a truncated download looks like.
    backup.write_bytes(raw[: len(raw) - 4096])
    with pytest.raises(RestoreRejected, match="integrity check"):
        inspect_backup(backup)

    response = hub.client.post(
        "/settings/restore", headers=auth(hub.token), **upload(backup)
    )

    assert "integrity check" in response.text
    assert_untouched(hub, response.status_code, response.text)


def test_restore_refuses_a_submission_without_the_confirmation(
    hub: ClaimedHub, tmp_path: Path
) -> None:
    backup, secrets = other_backup(tmp_path / "unconfirmed")

    response = hub.client.post(
        "/settings/restore", headers=auth(hub.token), **upload(backup, confirm=False)
    )

    assert "confirmation box" in response.text
    assert_untouched(hub, response.status_code, response.text)
    assert hub.hub.identity.verify_token(secrets.token) is False


def test_restore_refuses_a_submission_with_no_file(hub: ClaimedHub) -> None:
    response = hub.client.post(
        "/settings/restore", headers=auth(hub.token), data={"confirm": "on"}
    )
    assert "Choose a backup file" in response.text
    assert_untouched(hub, response.status_code, response.text)


def test_restore_over_the_cap_is_413(
    hub: ClaimedHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real cap is 64 MiB; the test moves it down rather than uploading
    one. Both halves of the guard read this module-level name, so patching
    it covers the Content-Length refusal and the byte counter alike."""
    monkeypatch.setattr("app.main.MAX_RESTORE_BYTES", 4096)
    backup, _ = other_backup(tmp_path / "oversized")
    assert backup.stat().st_size > 4096

    response = hub.client.post(
        "/settings/restore", headers=auth(hub.token), **upload(backup)
    )

    assert response.status_code == 413
    assert hub.hub.identity.verify_token(hub.token) is True
    assert sorted(p.name for p in hub.env.data_dir.glob("*.sqlite-tmp")) == []
    assert MAX_RESTORE_BYTES == 64 * 1024 * 1024


def test_restore_with_no_declared_length_is_refused_with_411(
    hub: ClaimedHub, tmp_path: Path
) -> None:
    """A chunked upload carries no Content-Length, so the fast refusal that
    reads that header can never fire, and ``request.form()`` no longer
    carries ``max_part_size=MAX_RESTORE_BYTES`` (starlette 1.6 spools a file
    part to disk with no size check of its own): without a declared length
    there would be nothing to stop the whole body landing on disk before the
    byte counter in ``_stream_upload_to`` ever ran. So the route refuses the
    request outright, before the multipart parser reads a byte."""
    backup, _ = other_backup(tmp_path / "chunked")
    boundary = "----deskmate-restore-test"
    payload = backup.read_bytes()

    def chunks() -> Iterator[bytes]:
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="confirm"\r\n\r\non\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="backup.sqlite"\r\n'
            "Content-Type: application/vnd.sqlite3\r\n\r\n"
        ).encode("ascii")
        yield payload
        yield f"\r\n--{boundary}--\r\n".encode("ascii")

    response = hub.client.post(
        "/settings/restore",
        headers={
            **auth(hub.token),
            "content-type": f"multipart/form-data; boundary={boundary}",
        },
        content=chunks(),
    )

    assert response.status_code == 411
    assert response.json()["detail"] == "restore needs a Content-Length"
    assert hub.hub.identity.verify_token(hub.token) is True
    assert sorted(p.name for p in hub.env.data_dir.glob("*.sqlite-tmp")) == []


def test_restore_refuses_an_oversized_text_field(hub: ClaimedHub, tmp_path: Path) -> None:
    """Starlette's own default part cap (1 MiB) is what a text field is
    bounded by now that this route no longer raises ``max_part_size`` to
    ``MAX_RESTORE_BYTES`` (that only ever affected text fields, never the
    file part - see the 411 test above). A field that blows past it must
    come back as neither a 200 nor a 500: whichever of 413/422 the existing
    ``MultiPartException`` handler produces is fine."""
    backup, _ = other_backup(tmp_path / "oversized-field")

    response = hub.client.post(
        "/settings/restore",
        headers=auth(hub.token),
        data={"confirm": "on", "padding": "x" * (4 * 1024 * 1024)},
        files={"file": (backup.name, backup.read_bytes(), "application/vnd.sqlite3")},
    )

    assert response.status_code not in (200, 500)
    assert hub.hub.identity.verify_token(hub.token) is True
    assert sorted(p.name for p in hub.env.data_dir.glob("*.sqlite-tmp")) == []


def test_the_upload_counter_refuses_past_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counter on its own: it counts the bytes it wrote, never the size
    the client claimed, and it stops the moment the total passes the cap."""
    monkeypatch.setattr("app.main.MAX_RESTORE_BYTES", 1024)
    target = tmp_path / "too-big.sqlite-tmp"
    upload = UploadFile(filename="big.sqlite", file=io.BytesIO(b"x" * 4096), size=1)

    with pytest.raises(HTTPException) as refusal:
        run(_stream_upload_to(upload, target))
    assert refusal.value.status_code == 413

    small = UploadFile(filename="small.sqlite", file=io.BytesIO(b"x" * 512), size=99999)
    assert run(_stream_upload_to(small, tmp_path / "small.sqlite-tmp")) == 512


def test_restore_needs_an_admin_credential(hub: ClaimedHub, tmp_path: Path) -> None:
    backup, _ = other_backup(tmp_path / "guarded")
    assert (
        hub.client.post(
            "/settings/restore", headers=auth(hub.device_key), **upload(backup)
        ).status_code
        == 401
    )
    assert hub.hub.identity.verify_token(hub.token) is True


# ---------------------------------------------------------------------------
# restore: the swap
# ---------------------------------------------------------------------------
def test_restore_swaps_the_database_and_reloads(hub: ClaimedHub, tmp_path: Path) -> None:
    backup, restored = other_backup(tmp_path / "good")
    admin_cookie = hub.admin_cookie()

    response = hub.client.post(
        "/settings/restore",
        headers=auth(hub.token),
        follow_redirects=False,
        **upload(backup),
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?restored=1&device_key_changed=1"
    # The cookie is deleted in the response: the secret that signed it is the
    # replaced database's.
    assert 'deskmate_session=""' in response.headers["set-cookie"]

    identity = hub.hub.identity
    assert identity.config is not None
    assert identity.config.name == "restored"
    assert identity.verify_token(restored.token) is True
    assert identity.verify_device_key(restored.device_key) is True
    assert identity.verify_token(hub.token) is False
    assert identity.verify_device_key(hub.device_key) is False

    # Nothing left over: the server's copy of the upload was moved into
    # place, not left beside the database.
    assert sorted(p.name for p in hub.env.data_dir.glob("*.sqlite-tmp")) == []

    # Every cookie this hub signed is dead, the admin's own included.
    stale = TestClient(hub.app, cookies={COOKIE_NAME: admin_cookie})
    refused = stale.get("/settings", follow_redirects=False)
    assert refused.status_code == 303
    assert refused.headers["location"].startswith("/login")

    # The restored database is live, not just the identity: the new token
    # reaches the settings page and a second backup reads the new hub row.
    assert hub.client.get("/settings", headers=auth(restored.token)).status_code == 200
    again = hub.client.post("/settings/backup", headers=auth(restored.token))
    assert again.status_code == 200
    copy = tmp_path / "again.sqlite"
    copy.write_bytes(again.content)
    assert inspect_backup(copy).device_key_sha256 == identity.config.device_key_sha256


def test_restore_of_this_hubs_own_backup_keeps_the_device_key_notice_off(
    hub: ClaimedHub, tmp_path: Path
) -> None:
    """Same device key in and out: no device_key_changed flag, so the login
    page does not tell the owner to go and reflash for nothing."""
    downloaded = hub.client.post("/settings/backup", headers=auth(hub.token))
    own = tmp_path / "own.sqlite"
    own.write_bytes(downloaded.content)

    response = hub.client.post(
        "/settings/restore",
        headers=auth(hub.token),
        follow_redirects=False,
        **upload(own),
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?restored=1"
    assert hub.hub.identity.verify_token(hub.token) is True


def test_the_login_page_shows_the_restore_notices(hub: ClaimedHub) -> None:
    plain = hub.client.get("/login")
    assert "restored from a backup" not in plain.text

    notified = hub.client.get("/login?restored=1&device_key_changed=1")
    assert "restored from a backup" in notified.text
    assert "different device key" in notified.text


def test_restore_while_a_render_is_in_flight(
    tmp_path: Path, renderer: Renderer, session_loop: asyncio.AbstractEventLoop
) -> None:
    """The close-replace-reopen sequence runs while a page render is still
    going: the render has to finish, and the new identity has to be the one
    in force afterwards.

    The render holds ``Hub._cache_lock`` for its whole duration and the swap
    takes the database lock, so this is also the deadlock check: neither
    waits on the other's lock in the wrong order.
    """
    live = ClaimedHub(tmp_path / "live")
    live.hub.renderer = renderer  # started on the session loop, unlike this app's own
    backup, restored = other_backup(tmp_path / "incoming")
    old_token = live.token

    async def scenario() -> bytes:
        state = await live.hub.state()
        render = asyncio.create_task(live.hub.png("today", state, force=True))
        # Let the render actually start before the file goes out from under it.
        await asyncio.sleep(0)
        async with live.hub.identity_lock:
            await run_in_threadpool(live.hub.db.replace_file, backup)
            await live.hub.reload()
        entry = await render
        return entry.png

    try:
        png = run(scenario())
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert live.hub.identity.verify_token(restored.token) is True
        assert live.hub.identity.verify_token(old_token) is False
        assert live.hub.identity.config is not None
        assert live.hub.identity.config.name == "restored"
    finally:
        live.hub.db.close()


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------
def test_rotate_replaces_both_secrets_and_every_session(hub: ClaimedHub) -> None:
    admin_cookie = hub.admin_cookie()
    config_before = hub.hub.identity.config
    assert config_before is not None

    response = hub.client.post(
        "/settings/rotate", headers=auth(hub.token), data={"confirm": "on"}
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    secrets = re.findall(r'<code id="(?:token|device-key)-value">([^<]+)</code>', response.text)
    assert len(secrets) == 2
    token, device_key = secrets
    assert token != hub.token
    assert device_key != hub.device_key

    identity = hub.hub.identity
    assert identity.verify_token(token) is True
    assert identity.verify_device_key(device_key) is True
    assert identity.verify_token(hub.token) is False
    assert identity.verify_device_key(hub.device_key) is False
    assert identity.config is not None
    # Everything that is not a secret survives.
    assert identity.config.name == config_before.name
    assert identity.config.base_url == config_before.base_url
    assert identity.config.created_at == config_before.created_at
    assert identity.config.session_secret != config_before.session_secret

    # The old admin cookie is dead; the fresh one in this response is not, so
    # whoever is reading the two secrets is not locked out of the page.
    stale = TestClient(hub.app, cookies={COOKIE_NAME: admin_cookie})
    assert stale.get("/settings", follow_redirects=False).status_code == 303
    fresh = response.cookies.get(COOKIE_NAME)
    assert fresh
    assert session_role(identity.config.session_secret, str(fresh), time.time()) == "admin"
    signed_in = TestClient(hub.app, cookies={COOKIE_NAME: str(fresh)})
    assert signed_in.get("/settings").status_code == 200


def test_rotate_without_the_confirmation_changes_nothing(hub: ClaimedHub) -> None:
    response = hub.client.post("/settings/rotate", headers=auth(hub.token), data={})

    assert response.status_code == 422
    assert "confirmation box" in response.text
    assert hub.hub.identity.verify_token(hub.token) is True
    assert hub.hub.identity.verify_device_key(hub.device_key) is True


def test_rotate_needs_an_admin_credential(hub: ClaimedHub) -> None:
    assert (
        hub.client.post(
            "/settings/rotate", headers=auth(hub.device_key), data={"confirm": "on"}
        ).status_code
        == 401
    )
    assert hub.hub.identity.verify_token(hub.token) is True


# ---------------------------------------------------------------------------
# the settings page itself
# ---------------------------------------------------------------------------
def test_the_settings_page_carries_the_three_forms_and_the_warnings(hub: ClaimedHub) -> None:
    page = hub.client.get("/settings", headers=auth(hub.token))

    assert page.status_code == 200
    for action in ("/settings/backup", "/settings/restore", "/settings/rotate"):
        assert f'action="{action}"' in page.text
    assert 'enctype="multipart/form-data"' in page.text
    assert page.text.count('name="confirm"') == 2
    assert "session secret" in page.text
    assert "DATA_DIR/modules" in page.text
    assert "Obsidian vault" in page.text
    assert "stops fetching" in page.text
    assert "data/deskmate.sqlite" in page.text


def _fill_with_telemetry(path: Path) -> None:
    """Pad a backup out to several pages of indexed rows, so corrupting it
    lands in real data rather than in an almost empty file's header."""
    rows = [
        (f"2026-09-19T00:00:{index % 60:02d}.000+00:00", f"device-{index:05d}")
        for index in range(3000)
    ]
    with closing(sqlite3.connect(str(path))) as connection:
        connection.executemany(
            "INSERT INTO telemetry (received_at, device) VALUES (?, ?)", rows
        )
        connection.commit()
        connection.execute("VACUUM")


def test_get_database_is_untouched_by_a_restore(hub: ClaimedHub, tmp_path: Path) -> None:
    """The registry entry survives the swap: the path never changed, so the
    process-wide lookup still hands back the very object Hub is holding."""
    backup, _ = other_backup(tmp_path / "registry")
    response = hub.client.post(
        "/settings/restore",
        headers=auth(hub.token),
        follow_redirects=False,
        **upload(backup),
    )
    assert response.status_code == 303
    assert get_database(hub.env.hub_db_file) is hub.hub.db
