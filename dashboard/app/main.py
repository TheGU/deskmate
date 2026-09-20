"""dashboard-hub FastAPI application.

Endpoints follow docs/ARCHITECTURE.md::

    GET    /healthz
    GET    /setup
    POST   /setup
    GET    /login
    POST   /login
    GET    /settings
    GET    /settings/geocode
    POST   /settings/{section}
    GET    /setup/{step}
    POST   /setup/{step}
    POST   /settings/backup
    POST   /settings/restore
    POST   /settings/rotate
    GET    /api/hub
    GET    /api/state
    POST   /api/ai-usage
    POST   /api/brief
    POST   /api/tasks
    GET    /display/{page}.png
    GET    /preview
    GET    /preview/{page}.html
    POST   /api/alert
    DELETE /api/alert
    POST   /api/device/telemetry
    GET    /api/device/telemetry
    GET    /api/device/history
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException

from app import __version__
from app.adapters.device import HISTORY_MAX_POINTS, device_status, downsample
from app.alerts import AlertStore
from app.backup import (
    BACKUP_MEDIA_TYPE,
    MAX_RESTORE_BYTES,
    RestoreRejected,
    backup_filename,
    backup_temp_path,
    inspect_backup,
    restore_temp_path,
)
from app.config import Env
from app.db import get_database
from app.forms import (
    FormErrors,
    SectionForm,
    errors_from_parse,
    form_errors,
    parse_section,
    render_section,
)
from app.geocode import (
    GeocodeFailed,
    PlaceSearch,
    clean_query,
    search as geocode_search,
)
from app.hub_config import (
    ADMIN_SESSION_MAX_AGE_SECONDS,
    AlreadyConfigured,
    COOKIE_NAME,
    HubIdentity,
    InvalidBaseURL,
    LoginRedirect,
    READER_SESSION_MAX_AGE_SECONDS,
    Role,
    SetupRedirect,
    _bearer_scheme,
    is_private_client_host,
    mint_session_cookie,
    reader_authenticated,
    require_admin,
    require_admin_html,
    require_device,
    require_reader,
    require_reader_html,
    require_token,
    rotate_secrets,
    validate_base_url,
    write_hub_config,
)
from app.legacy import LegacyEnv, import_legacy
from app.logging_setup import configure_logging, log
from app.models import (
    AlertRequest,
    DashboardState,
    DeviceSample,
    DeviceTelemetry,
)
from app.modules import Module, ModuleContext
from app.modules.registry import Registry, load_registry
from app.renderer.render import Renderer
from app.settings import SECTIONS, HubSettings, SettingsStore
from app.state import StateService, state_fingerprint
from app.telemetry import TelemetryStore, TelemetrySummary, utc_now
from app.timeutil import to_local

logger = logging.getLogger("app.main")

#: Matches uvicorn.run's own port in ``main()`` below; there is no
#: configurable bind host/port setting today.
DEFAULT_PORT = 8080

#: /setup and /login are the only POST routes with no bearer token: /setup
#: because that is how the hub's first credential is minted, /login because
#: it exchanges a credential for a session rather than requiring one
#: already. POST /setup is further restricted to a private/loopback caller
#: (see is_private_client_host in hub_config.py) while the hub is
#: unconfigured; every other route, including /api/device/telemetry, is 503
#: until then. Cap what either open POST route will buffer in memory before
#: validation ever runs.
MAX_OPEN_BODY_BYTES = 64 * 1024

#: How much of a restore upload is copied into DATA_DIR per hop through the
#: thread pool. Big enough that a 64 MiB file is a few hundred writes, small
#: enough that the byte counter refuses an oversized upload long before it is
#: all on disk.
UPLOAD_CHUNK_BYTES = 256 * 1024

#: The settings sections whose "Save and test" means something: each has a
#: ``source`` field and an adapter of the same name on ``StateService``
#: (state.py:StateService.adapters), which is what the test fetches. general
#: and alert have no source and so no test.
TESTABLE_SECTIONS: tuple[str, ...] = (
    "tasks",
    "calendar",
    "weather",
    "ai_usage",
    "brief",
    "home",
    "device",
)

#: The setup wizard's steps, in the plan's order. Every step is optional and
#: the last one's "next" is the settings page. The other five sections are
#: edited there: the wizard asks only for what a fresh hub needs to show
#: something real.
WIZARD_STEPS: tuple[str, ...] = ("general", "weather", "calendar", "home")

#: Cap on the two telemetry-origin strings (main.py:post_device_telemetry,
#: telemetry.py's remote_addr/hub_host columns): plenty for an IPv6 address
#: or a "https://host:port" base URL, short enough that a hostile Host
#: header cannot grow the row without bound.
TELEMETRY_ORIGIN_MAX_LEN = 200


@dataclass(slots=True)
class RenderCacheEntry:
    fingerprint: str
    png: bytes
    etag: str
    created_monotonic: float


def _require_writable_data_dir(path: Path) -> None:
    """Fail at startup with one clear line instead of at the first claim or
    push: a DATA_DIR the container cannot write to (wrong owner on a bind
    mount, most often) would otherwise only surface as a 500 on whichever
    request happens to write first.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = tempfile.NamedTemporaryFile(dir=path, prefix=".probe-", delete=False)
        probe.close()
        Path(probe.name).unlink()
    except OSError as exc:
        uid = getattr(os, "getuid", lambda: None)()
        uid_desc = f"uid {uid}" if uid is not None else "this user"
        raise RuntimeError(
            f"DATA_DIR {path} is not writable by {uid_desc}: {exc}. Make "
            "the directory owned by that uid, or set PUID/PGID in .env to "
            "the owner of the directory."
        ) from exc


def _seed_missing_sections(store: SettingsStore, seed: HubSettings) -> None:
    """Write every section of ``seed`` into ``store``, but only the ones the
    store has no row for yet.

    This is how ``create_app(env, hub_settings=...)`` lets a test hand the
    hub a ready-made ``HubSettings`` (fixture sources, mostly) without that
    seed ever clobbering a row a real deployment already saved through the
    settings page, or one ``import_legacy`` just wrote from the old
    environment: both of those ran first (``Hub.__init__`` calls this after
    ``import_legacy``), so a section either of them touched already has a
    row here and is left alone.
    """
    for section in SECTIONS:
        if store.updated_at(section) is None:
            store.save(section, getattr(seed, section))


class Hub:
    """Everything the request handlers need, built once at startup."""

    def __init__(self, env: Env, hub_settings: HubSettings | None = None) -> None:
        """``hub_settings``, when given, seeds the store's empty sections
        only (see :func:`_seed_missing_sections`): it never overwrites a row
        that is already there. The hub's real settings are always the
        store's own snapshot, read fresh after any seeding.
        """
        _require_writable_data_dir(env.data_dir)
        self.env = env
        # The database is process-wide and keyed by path (app/db.py), so two
        # create_app() calls over one DATA_DIR share one connection instead of
        # racing through two. Hub holds the reference; it does not own the
        # lifetime, which is what makes a restore able to swap the file.
        self.db = get_database(env.hub_db_file)
        self.db.migrate()
        # A caller that hands in a seed (tests, scripts/render-all.py) owns
        # the settings: the legacy import then reads only real environment
        # variables, never a developer's .env file lying next to the repo,
        # or that file's sources would win over the seed on a fresh DATA_DIR.
        legacy_env = LegacyEnv(_env_file=None) if hub_settings is not None else LegacyEnv()
        import_legacy(self.db, legacy_env, env.data_dir)
        self.settings_store = SettingsStore(self.db)
        if hub_settings is not None:
            _seed_missing_sections(self.settings_store, hub_settings)
        self.hub_settings = self.settings_store.snapshot()
        # The registry is built from the snapshot, so the ``modules`` section
        # (which module is enabled, in what order) is what decides which
        # pages this hub serves and which adapters it runs. It is rebuilt on
        # every reload, which is how a settings save takes effect.
        self.registry = self._build_registry()
        self.alerts = AlertStore(self.db, self.hub_settings.general.timezone)
        self.telemetry = TelemetryStore(self.db, self.hub_settings.device.retention_days)
        self.state_service = StateService(
            self.hub_settings, env, self.alerts, self.registry, self.db
        )
        self.renderer = Renderer(env, self.hub_settings, self.registry)
        self.identity = HubIdentity(self.db)
        self._cache: dict[str, RenderCacheEntry] = {}
        self._cache_lock = asyncio.Lock()
        # Serializes the two flows that rewrite the hub's identity out from
        # under everything else: restore (close, replace the file, reopen,
        # reload) and rotate. Like HubIdentity's claim lock it is an
        # asyncio.Lock on this object, so it serializes within this process,
        # which is all a single-worker deployment needs (see the workers=1
        # note by the uvicorn command in Dockerfile).
        self.identity_lock = asyncio.Lock()
        if not self.identity.configured:
            log(
                logger,
                logging.WARNING,
                "Hub not set up: open /setup on this hub's address now; until then it "
                "serves nothing else",
            )

    async def reload(self) -> None:
        """Rebuild everything that reads the database, after it changed.

        Used by the flows that rewrite the hub's own rows (rotate, restore,
        and a settings save in 1.4). The identity comes back from the ``hub``
        row, the settings snapshot is re-read from ``self.settings_store``
        (so a section saved through the store since the last read is what
        this reload picks up), the state service and the renderer's own
        snapshot are rebuilt from it, the alert store re-reads its row (never
        resets: whatever the panel is showing has to survive a settings
        save) and is pointed at the freshly saved ``general.timezone`` (also
        never dropping the currently-showing alert, see
        ``AlertStore.set_timezone``), and the render cache is dropped under
        ``_cache_lock`` so a render already in flight finishes on the old
        service while the next request sees the new one.
        """
        identity = await run_in_threadpool(HubIdentity, self.db)
        self.identity = identity
        await run_in_threadpool(self.alerts.load)
        self.hub_settings = await run_in_threadpool(self.settings_store.snapshot)
        self.alerts.set_timezone(self.hub_settings.general.timezone)
        # TelemetryStore owns no connection of its own (see __init__), but it
        # does carry retention_days as a plain attribute read by the
        # /api/device/telemetry summary: without rebuilding it here, a saved
        # device.retention_days would never reach that response.
        self.telemetry = TelemetryStore(self.db, self.hub_settings.device.retention_days)
        self.registry = await run_in_threadpool(self._build_registry)
        self.state_service = StateService(
            self.hub_settings, self.env, self.alerts, self.registry, self.db
        )
        self.renderer.hub_settings = self.hub_settings
        self.renderer.registry = self.registry
        async with self._cache_lock:
            self._cache.clear()
        log(logger, logging.INFO, "hub reloaded", configured=self.identity.configured)

    def _build_registry(self) -> Registry:
        """Every module this hub can see, with the ``modules`` section applied.

        Built-ins, then ``deskmate.modules`` entry points, then the packages
        under ``DATA_DIR/modules/``. A duplicate or invalid id refuses to
        load rather than being dropped, so a hub either serves what the
        settings page says it serves or does not come up.
        """
        return load_registry(self.hub_settings.modules, self.env.data_dir)

    def module_context(self, module: Module) -> ModuleContext:
        """What this hub hands ``module`` when it builds a router."""
        return ModuleContext(
            env=self.env,
            db=self.db,
            data_dir=self.env.data_dir,
            http_timeout_seconds=self.env.http_timeout_seconds,
            logger=logging.getLogger(f"app.modules.{module.id}"),
        )

    def has_page(self, page: str) -> bool:
        """Whether this hub draws ``page``: an enabled module's, or alert."""
        return page in self.renderer.pages

    async def state(self, *, force: bool = False) -> DashboardState:
        return await self.state_service.build(force=force)

    async def png(self, page: str, state: DashboardState, *, force: bool) -> RenderCacheEntry:
        """Return the cached PNG for ``page`` or render a fresh one."""
        fingerprint = state_fingerprint(state)
        # Alert is not a module, so the registry has no TTL for it: it is an
        # interrupt and is re-rendered whenever it is asked for.
        ttl = self.registry.page_ttl_seconds(page)
        async with self._cache_lock:
            entry = self._cache.get(page)
            if (
                entry is not None
                and not force
                and entry.fingerprint == fingerprint
                and (time.monotonic() - entry.created_monotonic) < ttl
            ):
                log(logger, logging.DEBUG, "png cache hit", page=page)
                return entry
            png = await self.renderer.render_png(page, state)
            fresh = RenderCacheEntry(
                fingerprint=fingerprint,
                png=png,
                etag='"' + hashlib.sha256(png).hexdigest() + '"',
                created_monotonic=time.monotonic(),
            )
            self._cache[page] = fresh
            return fresh


def etag_matches(header: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match comparison, weak-tag tolerant."""
    if not header:
        return False
    bare = etag.strip('"')
    for candidate in header.split(","):
        token = candidate.strip()
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:]
        if token.strip('"') == bare:
            return True
    return False


async def _read_capped_body(request: Request) -> bytes:
    """Read ``request``'s body in chunks, rejecting it once it passes
    ``MAX_OPEN_BODY_BYTES`` instead of buffering an arbitrarily large one.

    Sets ``request._body`` on the way out (the same attribute
    ``Request.body()`` caches), so a route that goes on to call
    ``request.form()`` or ``request.json()`` reuses this read instead of
    trying to consume the already-drained stream again.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_OPEN_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body  # noqa: SLF001 - see docstring
    return body


async def _cap_form_body(request: Request) -> None:
    """The Content-Length guard shared by POST /setup and POST /login:
    ``request.form()`` reads ``request.stream()`` itself, so it cannot be
    handed ``_read_capped_body``'s bytes directly. When Content-Length is
    present, reject an oversized body before ``form()`` ever touches the
    stream; when it is absent (e.g. chunked), read the capped body first so
    it is cached on ``request._body``, which ``stream()`` (and so ``form()``)
    reuses.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > MAX_OPEN_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body too large")
    else:
        await _read_capped_body(request)


def _cap_restore_length(request: Request) -> None:
    """The declared-size half of the restore cap, checked before the
    multipart parser reads a byte.

    Content-Length is a claim, not a fact, so it is only ever a fast refusal:
    :func:`_stream_upload_to`'s counter is what actually decides. A request
    with no Content-Length (chunked, most often) is refused outright here
    instead: ``request.form()`` no longer carries ``max_part_size=
    MAX_RESTORE_BYTES`` (that only ever raised the *text*-field cap; a
    starlette 1.6 file part is spooled to disk with no size check of its
    own), so without a declared length there is nothing to stop the whole
    body being read into a spooled temp file before the byte counter in
    :func:`_stream_upload_to` ever sees it.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        raise HTTPException(status_code=411, detail="restore needs a Content-Length")
    try:
        declared_length = int(content_length)
    except ValueError:
        return
    if declared_length > MAX_RESTORE_BYTES:
        raise HTTPException(status_code=413, detail="the uploaded file is too large")


async def _stream_upload_to(upload: UploadFile, target: Path) -> int:
    """Copy ``upload`` into ``target`` a chunk at a time, returning the byte
    count and raising 413 the moment it passes :data:`MAX_RESTORE_BYTES`.

    The count comes from the bytes actually written, never from
    ``UploadFile.size`` or a Content-Length: both are the client's word for
    it. Each write goes through the thread pool, the same rule every other
    synchronous file write in this module follows.
    """
    written = 0
    with target.open("wb") as handle:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_RESTORE_BYTES:
                raise HTTPException(status_code=413, detail="the uploaded file is too large")
            await run_in_threadpool(handle.write, chunk)
    return written


def _delete_quietly(path: Path) -> None:
    """Remove a temp file, logging rather than raising when it will not go:
    it runs as a response background task, where an exception would only
    reach the server log anyway, long after the body was sent."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log(logger, logging.WARNING, "temp file not removed", path=str(path), error=str(exc))


def _section_form(
    hub: "Hub",
    section: str,
    *,
    values: dict[str, Any] | None = None,
    errors: FormErrors | None = None,
    search: PlaceSearch | None = None,
    search_url: str = "/settings/geocode",
) -> SectionForm:
    """One section's form, generated from its model (``app/forms.py``).

    ``values`` is what fills the inputs: the stored section by default, or a
    rejected submission's own values when the page is being re-rendered with
    its errors, so nobody retypes a whole form because one field was wrong.
    """
    model = SECTIONS[section]
    current = getattr(hub.hub_settings, section)
    form = render_section(
        section,
        model,
        current.model_dump() if values is None else values,
        errors=errors,
    )
    if section == "weather":
        # The place search exists only for weather: it is what turns a place
        # name into the latitude and longitude that section stores.
        form.search = search if search is not None else PlaceSearch(url=search_url)
    return form


def _settings_forms(hub: "Hub", *, replace: SectionForm | None = None) -> list[SectionForm]:
    """Every section's form in SECTIONS order, with ``replace`` swapped in for
    its own section (the one just saved, tested or refused)."""
    return [
        replace
        if replace is not None and replace.section == section
        else _section_form(hub, section)
        for section in SECTIONS
    ]


def _settings_html(
    hub: "Hub",
    *,
    error: str | None = None,
    status_code: int = 200,
    forms: list[SectionForm] | None = None,
) -> HTMLResponse:
    """The settings page: one form per section, then backup, restore and the
    danger zone, optionally carrying one error line.

    Shared by GET /settings and by the POSTs that refuse a submission (a
    browser form gets the page back with the reason, not a JSON detail).
    """
    config = hub.identity.config
    assert config is not None
    template = hub.renderer.environment.get_template("settings.html")
    html = template.render(
        name=config.name,
        error=error,
        forms=_settings_forms(hub) if forms is None else forms,
    )
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})


def _apply_place(values: dict[str, Any], form: FormData) -> None:
    """Fold a chosen search result into the weather section's values.

    The radio carries ``"<latitude>,<longitude>,<name>"``
    (``app/geocode.py:Place.value``), split at most twice so a place name with
    a comma in it survives. A malformed value is ignored rather than raised
    on: it can only come from a hand-made request, and the three fields it
    would have filled are right there to type into.
    """
    raw = form.get("place")
    if not isinstance(raw, str) or not raw.strip():
        return
    parts = raw.strip().split(",", 2)
    if len(parts) != 3:
        return
    latitude, longitude, name = parts
    values["latitude"] = latitude.strip()
    values["longitude"] = longitude.strip()
    values["location_name"] = name.strip()


async def _save_section(
    hub: "Hub", section: str, form: FormData, *, search_url: str
) -> SectionForm | None:
    """Validate and store one settings section, then reload the hub.

    Returns ``None`` when it saved. Otherwise it returns that section's form
    carrying what was submitted plus the messages against the inputs that
    caused them: the caller re-renders it with a 422, so a browser sees
    exactly which field it has to fix.
    """
    model = SECTIONS[section]
    current = getattr(hub.hub_settings, section)
    parsed = parse_section(model, form, current)
    if section == "weather":
        _apply_place(parsed.data, form)
    if parsed.errors:
        return _section_form(
            hub,
            section,
            values=parsed.data,
            errors=errors_from_parse(parsed),
            search_url=search_url,
        )
    try:
        value = model.model_validate(parsed.data)
    except ValidationError as exc:
        return _section_form(
            hub,
            section,
            values=parsed.data,
            errors=form_errors(model, exc),
            search_url=search_url,
        )
    await run_in_threadpool(hub.settings_store.save, section, value)
    # The snapshot every adapter, page and route reads is rebuilt here: that
    # is what makes the next render use what was just saved.
    await hub.reload()
    log(logger, logging.INFO, "settings section saved", section=section)
    return None


async def _tested_section_form(hub: "Hub", section: str, *, search_url: str) -> SectionForm:
    """The section's form with one forced adapter fetch reported on it.

    That is what "Save and test" is for: the Outcome's status and error
    string (``adapters/base.py:Outcome``) are what tell the owner an ICS URL
    or a Home Assistant token is wrong, on the page, before they move on.
    """
    form = _section_form(hub, section, search_url=search_url)
    adapter = hub.state_service.adapters[section]
    outcome = await adapter.get(force=True)
    form.test_status = outcome.status.value
    form.test_error = outcome.error or ""
    log(logger, logging.INFO, "settings section tested", section=section, status=form.test_status)
    return form


async def _place_search(hub: "Hub", raw_query: str, url: str) -> PlaceSearch:
    """Run the weather section's place search, never raising.

    An upstream failure is one line under the Find box, never a 500: the
    search is a convenience and the coordinates can always be typed in. The
    query itself is never logged, here or in ``app/geocode.py``.
    """
    query = clean_query(raw_query)
    if not query:
        return PlaceSearch(url=url)
    try:
        places = await geocode_search(query, hub.env.http_timeout_seconds)
    except GeocodeFailed as exc:
        return PlaceSearch(url=url, query=query, error=str(exc))
    if not places:
        return PlaceSearch(url=url, query=query, error="No place matched that name.")
    return PlaceSearch(url=url, query=query, places=places)


def _wizard_next(step: str) -> str:
    """Where "Skip" and a saved step go: the next step, then /settings."""
    index = WIZARD_STEPS.index(step)
    if index + 1 < len(WIZARD_STEPS):
        return f"/setup/{WIZARD_STEPS[index + 1]}"
    return "/settings"


def _wizard_html(
    hub: "Hub", form: SectionForm, step: str, *, status_code: int = 200
) -> HTMLResponse:
    """One wizard step: the same section form the settings page renders,
    alone, with "Save and continue" and a "Skip" link to the next step."""
    template = hub.renderer.environment.get_template("wizard.html")
    html = template.render(
        form=form,
        step_number=WIZARD_STEPS.index(step) + 1,
        step_total=len(WIZARD_STEPS),
        skip_url=_wizard_next(step),
    )
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})


def _validation_problems(error: ValidationError) -> list[dict[str, str]]:
    """Pydantic errors flattened to something JSON-safe and short."""
    return [
        {
            "field": ".".join(str(part) for part in item["loc"]) or "body",
            "error": str(item["msg"]),
        }
        for item in error.errors(include_url=False)[:5]
    ]


def _stamp(value: datetime | None, timezone_name: str) -> str | None:
    """UTC storage stamp presented in TIMEZONE."""
    return None if value is None else to_local(value, timezone_name).isoformat()


def _sample_json(sample: DeviceSample, timezone_name: str) -> dict[str, Any]:
    payload = sample.model_dump(mode="json")
    payload["received_at"] = _stamp(sample.received_at, timezone_name)
    return payload


def _read_latest(hub: "Hub") -> tuple[DeviceSample | None, TelemetrySummary]:
    """One thread-pool hop for the two store reads the latest endpoint needs."""
    return hub.telemetry.latest(), hub.telemetry.summary()


def _forwarded_scheme(request: Request) -> str:
    """The scheme to treat ``request`` as: the first value of a comma
    separated ``X-Forwarded-Proto`` (a chain of proxies lists the original
    client's scheme first, e.g. ``"https, http"``), or the request's own
    scheme when the header is absent. Shared by the setup page's base-URL
    guess and :func:`_telemetry_origin`'s ``hub_host``, so the two never
    disagree about which proxy header value to trust.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if not forwarded_proto:
        return request.url.scheme
    return forwarded_proto.split(",")[0].strip()


def _telemetry_origin(request: Request) -> tuple[str | None, str | None]:
    """``(remote_addr, hub_host)`` for a telemetry POST: who sent it, and the
    hub URL they used to reach it. Both capped and both best-effort - neither
    is trusted input, and a caller behind a reverse proxy is free to omit or
    spoof ``X-Forwarded-Proto``/``Host``, so this is a display convenience
    (System page HUB column) never a security control.
    """
    remote_addr = None if request.client is None else request.client.host
    if remote_addr is not None:
        remote_addr = remote_addr[:TELEMETRY_ORIGIN_MAX_LEN]

    scheme = _forwarded_scheme(request)
    host = request.headers.get("host") or ""
    try:
        hub_host = validate_base_url(f"{scheme}://{host}")[:TELEMETRY_ORIGIN_MAX_LEN]
    except InvalidBaseURL:
        hub_host = None
    return remote_addr, hub_host


def create_app(env: Env | None = None, hub_settings: HubSettings | None = None) -> FastAPI:
    """Build the FastAPI app and the one :class:`Hub` behind it.

    ``hub_settings`` is a seed for tests, not the hub's real settings: any
    section it carries is written into the settings store only if that
    section has no row there yet (see :func:`_seed_missing_sections`), so a
    seed can never clobber a row a real deployment already saved or one
    ``import_legacy`` just wrote from the old environment. What every route
    actually reads is always ``Hub.hub_settings``, the store's own snapshot,
    taken fresh right after any seeding.
    """
    env = env or Env()
    configure_logging(env.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub: Hub = app.state.hub
        log(
            logger,
            logging.INFO,
            "dashboard-hub starting",
            version=__version__,
            timezone=hub.hub_settings.general.timezone,
            fixtures=str(hub.env.fixtures_dir),
            tasks=hub.hub_settings.tasks.source,
            calendar=hub.hub_settings.calendar.source,
            weather=hub.hub_settings.weather.source,
            ai_usage=hub.hub_settings.ai_usage.source,
            brief=hub.hub_settings.brief.source,
            home=hub.hub_settings.home.source,
            device=hub.hub_settings.device.source,
            database=str(hub.env.hub_db_file),
        )
        await hub.renderer.start()
        try:
            yield
        finally:
            await hub.renderer.close()
            # The database is process-wide and may be shared with another app
            # instance (tests build several), so shutdown leaves it open.
            # Every write commits, so nothing is lost when the process exits.
            log(logger, logging.INFO, "dashboard-hub stopped")

    app = FastAPI(title="dashboard-hub", version=__version__, lifespan=lifespan)
    app.state.hub = Hub(env, hub_settings)
    if env.static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(env.static_dir)), name="static")

    # -- health ----------------------------------------------------------
    #: A healthz-only word for an adapter that has never fetched yet.
    #: Deliberately not an AdapterStatus member: calling that "error" or
    #: "unavailable" before anything has even tried would be a lie.
    HEALTH_STATUS_UNKNOWN = "unknown"

    @app.get("/healthz")
    async def healthz(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    ) -> JSONResponse:
        """Liveness check: answers from each adapter's last known outcome
        and never fetches, so an outage cannot make this route slow (see the
        CachedAdapter failure backoff in adapters/base.py). GET /api/state
        is what forces every adapter to fetch and reports live status.

        Unconfigured, or configured but without a reader credential (token,
        device key, or session cookie): only status/version/renderer, so an
        unauthenticated probe from the LAN cannot tell an unset-up hub from
        a configured one, let alone enumerate adapter names or alert state.
        A reader on a configured hub gets the full body.
        """
        hub: Hub = app.state.hub
        minimal: dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "renderer": {"connected": hub.renderer.connected},
        }
        if not reader_authenticated(request, credentials):
            return JSONResponse(minimal)
        adapters: dict[str, dict[str, Any]] = {}
        for name, cached in hub.state_service.adapters.items():
            outcome = cached.last_outcome
            if outcome is None:
                adapters[name] = {
                    "status": HEALTH_STATUS_UNKNOWN,
                    "source": cached.source,
                    "updated_at": None,
                    "error": None,
                }
            else:
                adapters[name] = {
                    "status": outcome.status.value,
                    "source": outcome.source,
                    "updated_at": outcome.updated_at.isoformat() if outcome.updated_at else None,
                    "error": outcome.error,
                }
        return JSONResponse(
            {
                **minimal,
                "timezone": hub.hub_settings.general.timezone,
                "pages": list(hub.renderer.pages),
                "adapters": adapters,
                "alert": hub.alerts.current.priority.value if hub.alerts.current else None,
            }
        )

    # -- state -----------------------------------------------------------
    @app.get("/api/state", dependencies=[Depends(require_reader)])
    async def api_state(request: Request) -> Response:
        hub: Hub = app.state.hub
        state = await hub.state(force="t" in request.query_params)
        return Response(
            content=state.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    # -- hub identity ------------------------------------------------------
    @app.get("/api/hub", dependencies=[Depends(require_reader)])
    async def api_hub() -> JSONResponse:
        hub: Hub = app.state.hub
        config = hub.identity.config
        return JSONResponse(
            {
                "name": config.name if config else None,
                "base_url": config.base_url if config else None,
                "configured": hub.identity.configured,
                "version": __version__,
                "timezone": hub.hub_settings.general.timezone,
                # Straight from the section models: "push" always reads the
                # matching ``datasets`` row (app/datasets.py) directly, so
                # there is no live/last-fetch split left to report (that was
                # the "auto" selector's own story, dropped per the plan's
                # Non-goals).
                "sources": {
                    "ai_usage": {"source": hub.hub_settings.ai_usage.source},
                    "brief": {"source": hub.hub_settings.brief.source},
                    "tasks": {"source": hub.hub_settings.tasks.source},
                },
            }
        )

    # -- pushed data -------------------------------------------------------
    # A module that declares routes hands core an APIRouter; core mounts it
    # under /api with the bearer-token dependency applied, so a module
    # cannot forget auth. Each push route writes its dataset row in a
    # threadpool and invalidates the matching CachedAdapter so /api/state
    # reflects it on the very next build, not after the adapter's own TTL
    # (app/modules/<id>/routes.py).
    #
    # Every installed module, not only the enabled ones: routes are fixed at
    # startup (FastAPI has no unmount), so mounting only the enabled ones
    # would mean a restart after every enable, while leaving a disabled
    # module's route up costs nothing - it writes a row nothing draws and
    # says exactly that in its own "warning" field.
    for module in app.state.hub.registry.modules:
        if module.routes is None:
            continue
        app.include_router(
            module.routes(app.state.hub.module_context(module)),
            prefix="/api",
            dependencies=[Depends(require_token)],
        )

    # -- alerts ------------------------------------------------------------
    @app.post("/api/alert", dependencies=[Depends(require_token)])
    async def post_alert(payload: AlertRequest) -> JSONResponse:
        hub: Hub = app.state.hub
        if payload.duration_seconds is None:
            payload = payload.model_copy(
                update={"duration_seconds": hub.hub_settings.alert.default_duration_seconds}
            )
        alert, accepted = hub.alerts.set(payload)
        return JSONResponse(
            status_code=201 if accepted else 409,
            content={
                "accepted": accepted,
                "alert": alert.model_dump(mode="json"),
                "reason": None if accepted else "an alert with higher priority is active",
            },
        )

    @app.delete("/api/alert", dependencies=[Depends(require_token)])
    async def delete_alert() -> JSONResponse:
        hub: Hub = app.state.hub
        cleared = hub.alerts.clear()
        return JSONResponse({"cleared": cleared})

    # -- device telemetry ------------------------------------------------
    # firmware/e1002.yaml sends "Authorization: Bearer ${hub_key}" on this
    # POST, ${hub_key} being the device key /setup hands out. require_device
    # 503s until the hub is set up (there is no key to send yet), then
    # requires it; older firmware without the header gets 401 rather than a
    # silent accept.
    @app.post("/api/device/telemetry", dependencies=[Depends(require_device)])
    async def post_device_telemetry(request: Request) -> JSONResponse:
        hub: Hub = app.state.hub
        body = await _read_capped_body(request)
        try:
            payload: Any = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            log(logger, logging.WARNING, "telemetry rejected", reason="body is not JSON")
            return JSONResponse(
                {"accepted": False, "error": "request body is not valid JSON"},
                status_code=400,
            )
        try:
            telemetry = DeviceTelemetry.model_validate(payload)
        except ValidationError as exc:
            problems = _validation_problems(exc)
            log(logger, logging.WARNING, "telemetry rejected", reason="invalid", detail=problems)
            return JSONResponse(
                {"accepted": False, "error": "invalid telemetry payload", "detail": problems},
                status_code=400,
            )

        remote_addr, hub_host = _telemetry_origin(request)
        received_at = await run_in_threadpool(
            hub.telemetry.insert,
            telemetry,
            remote_addr=remote_addr,
            hub_host=hub_host,
        )
        # The next page render must see this sample, not the cached one.
        hub.state_service.adapters["device"].invalidate()
        return JSONResponse(
            {
                "accepted": True,
                "received_at": to_local(received_at, hub.hub_settings.general.timezone).isoformat(),
            },
            status_code=202,
        )

    @app.get("/api/device/telemetry", dependencies=[Depends(require_reader)])
    async def get_device_telemetry() -> JSONResponse:
        hub: Hub = app.state.hub
        latest, summary = await run_in_threadpool(_read_latest, hub)
        age = None if latest is None else round((utc_now() - latest.received_at).total_seconds(), 1)
        timezone_name = hub.hub_settings.general.timezone
        return JSONResponse(
            {
                "status": device_status(age).value,
                "source": hub.hub_settings.device.source,
                "age_seconds": age,
                "latest": None if latest is None else _sample_json(latest, timezone_name),
                "summary": {
                    "sample_count": summary.sample_count,
                    "oldest": _stamp(summary.oldest, timezone_name),
                    "newest": _stamp(summary.newest, timezone_name),
                    "retention_days": hub.telemetry.retention_days,
                },
            },
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/device/history", dependencies=[Depends(require_reader)])
    async def get_device_history(
        hours: float = Query(default=24.0, gt=0.0, le=8760.0),
    ) -> JSONResponse:
        hub: Hub = app.state.hub
        samples = await run_in_threadpool(hub.telemetry.history, hours)
        points = downsample(samples, HISTORY_MAX_POINTS)
        timezone_name = hub.hub_settings.general.timezone
        return JSONResponse(
            {
                "hours": hours,
                "sample_count": len(samples),
                "point_count": len(points),
                "max_points": HISTORY_MAX_POINTS,
                "downsampled": len(points) < len(samples),
                "samples": [_sample_json(sample, timezone_name) for sample in points],
            },
            headers={"Cache-Control": "no-cache"},
        )

    # -- display ---------------------------------------------------------
    @app.get("/display/{page}.png", dependencies=[Depends(require_reader)])
    async def display(page: str, request: Request) -> Response:
        hub: Hub = app.state.hub
        # 2.1b adds /display/{n}.png, which resolves an integer n to the
        # n-th enabled page's id (registry.page_by_index) before the render
        # cache, so the cache key and the X-Deskmate-Page header stay the id.
        if not hub.has_page(page):
            return JSONResponse({"error": f"unknown page {page}"}, status_code=404)
        force = "t" in request.query_params
        state = await hub.state(force=force)
        entry = await hub.png(page, state, force=force)
        headers = {
            "ETag": entry.etag,
            "Cache-Control": "no-cache",
            "X-Deskmate-Page": page,
        }
        if etag_matches(request.headers.get("if-none-match"), entry.etag):
            log(logger, logging.INFO, "display not modified", page=page)
            return Response(status_code=304, headers=headers)
        headers["Content-Length"] = str(len(entry.png))
        return Response(content=entry.png, media_type="image/png", headers=headers)

    # -- setup ------------------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    async def get_setup(request: Request) -> HTMLResponse:
        hub: Hub = app.state.hub
        if hub.identity.error is not None:
            raise HTTPException(status_code=503, detail=hub.identity.error)
        if hub.identity.configured:
            template = hub.renderer.environment.get_template("setup-configured.html")
            return HTMLResponse(template.render())
        scheme = _forwarded_scheme(request)
        guessed_base_url = f"{scheme}://{request.headers.get('host', '')}"
        template = hub.renderer.environment.get_template("setup.html")
        html = template.render(default_name="deskmate", default_base_url=guessed_base_url)
        return HTMLResponse(html)

    @app.post("/setup", response_class=HTMLResponse)
    async def post_setup(request: Request) -> HTMLResponse:
        hub: Hub = app.state.hub
        if hub.identity.error is not None:
            raise HTTPException(status_code=503, detail=hub.identity.error)
        if not hub.identity.configured:
            # No claim code any more: a caller off the local network is
            # refused outright, rather than allowed to race for the claim.
            client_host = request.client.host if request.client is not None else None
            if not is_private_client_host(client_host):
                raise HTTPException(
                    status_code=403, detail="setup is only allowed from the local network"
                )
        await _cap_form_body(request)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        base_url = str(form.get("base_url", ""))
        # Capped before claim_hub's own base-URL validation runs, so an
        # absurd or empty name/base_url is a plain 422, not whatever urlsplit
        # or a downstream write does with it.
        if not name or len(name) > 60:
            raise HTTPException(status_code=422, detail="name must be 1 to 60 characters")
        if len(base_url) > 200:
            raise HTTPException(status_code=422, detail="base_url must be 200 characters or fewer")
        try:
            secrets = await hub.identity.claim(name=name, base_url=base_url)
        except AlreadyConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidBaseURL as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config = hub.identity.config
        assert config is not None
        template = hub.renderer.environment.get_template("setup-done.html")
        html = template.render(
            name=config.name,
            base_url=config.base_url,
            token=secrets.token,
            device_key=secrets.device_key,
            skill_path="skills/deskmate/SKILL.md",
        )
        # This page carries both secrets, shown once: never cache or store it.
        response = HTMLResponse(
            html, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )
        # The claimer was just shown the token on this very page, so signing
        # them in as admin here adds no exposure and saves them pasting it
        # straight back in at /login.
        response.set_cookie(
            COOKIE_NAME,
            mint_session_cookie(config.session_secret, time.time(), "admin"),
            max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
            secure=config.base_url.startswith("https"),
        )
        return response

    # -- login -------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    async def get_login(request: Request) -> Response:
        hub: Hub = app.state.hub
        if hub.identity.error is not None:
            raise HTTPException(status_code=503, detail=hub.identity.error)
        if not hub.identity.configured:
            return RedirectResponse("/setup", status_code=303)
        next_path = request.query_params.get("next", "/preview")
        template = hub.renderer.environment.get_template("login.html")
        # POST /settings/restore lands here with its cookie deleted and these
        # two flags set: the page is the only place left to tell the admin
        # that the credentials they had are the backup's now, and whether the
        # flashed device needs its key updated too.
        html = template.render(
            next=next_path,
            error=None,
            restored="restored" in request.query_params,
            device_key_changed="device_key_changed" in request.query_params,
        )
        return HTMLResponse(html)

    @app.post("/login", response_class=HTMLResponse)
    async def post_login(request: Request) -> Response:
        hub: Hub = app.state.hub
        if hub.identity.error is not None:
            raise HTTPException(status_code=503, detail=hub.identity.error)
        if not hub.identity.configured:
            return RedirectResponse("/setup", status_code=303)
        await _cap_form_body(request)
        form = await request.form()
        key = str(form.get("key", "")).strip()
        submitted_next = str(form.get("next", ""))
        # An open redirect target ("//evil.example" parses as scheme-relative
        # by every browser) must never come back out of this form unchecked.
        next_path = (
            submitted_next
            if submitted_next.startswith("/") and not submitted_next.startswith("//")
            else "/preview"
        )
        config = hub.identity.config
        assert config is not None
        # The token signs in as admin, the device key as reader - two
        # independent credentials, so this checks each explicitly rather
        # than the combined verify_reader (which cannot say which one
        # matched).
        role: Role
        if key and hub.identity.verify_token(key):
            role = "admin"
        elif key and hub.identity.verify_device_key(key):
            role = "reader"
        else:
            template = hub.renderer.environment.get_template("login.html")
            html = template.render(
                next=next_path, error="wrong key", restored=False, device_key_changed=False
            )
            return HTMLResponse(html, status_code=401)
        max_age = ADMIN_SESSION_MAX_AGE_SECONDS if role == "admin" else READER_SESSION_MAX_AGE_SECONDS
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            mint_session_cookie(config.session_secret, time.time(), role),
            max_age=max_age,
            httponly=True,
            samesite="lax",
            path="/",
            secure=config.base_url.startswith("https"),
        )
        return response

    # -- settings ----------------------------------------------------------
    @app.get(
        "/settings", response_class=HTMLResponse, dependencies=[Depends(require_admin_html)]
    )
    async def settings_page(request: Request) -> HTMLResponse:
        """Every section as its own form, then backup, restore and rotate.

        ``?saved=<section>`` is what a save redirects back to (together with
        the ``#<section>`` fragment, which is what puts the browser back
        where it was): the notice cannot ride on the redirect any other way
        without a session store, and this one says nothing a query string
        should not carry.
        """
        hub: Hub = app.state.hub
        forms = _settings_forms(hub)
        saved = request.query_params.get("saved", "")
        for form in forms:
            if form.section == saved:
                form.saved = True
        return _settings_html(hub, forms=forms)

    @app.get(
        "/settings/geocode",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_html)],
    )
    async def settings_geocode(request: Request) -> HTMLResponse:
        """The settings page with the weather section's search results on it.

        A plain GET form with one ``q`` field, so the whole flow is a link
        and a page: no JavaScript, and nothing is saved until the admin picks
        a result and presses Save.
        """
        hub: Hub = app.state.hub
        search = await _place_search(
            hub, request.query_params.get("q", ""), "/settings/geocode"
        )
        weather = _section_form(hub, "weather", search=search)
        return _settings_html(hub, forms=_settings_forms(hub, replace=weather))

    @app.post("/settings/backup", dependencies=[Depends(require_admin)])
    async def post_settings_backup() -> Response:
        """Download the whole hub as one SQLite file.

        ``VACUUM INTO`` writes a fresh consistent copy next to the live
        database (never a plain file copy: the live one has a WAL beside it),
        the copy is streamed out as an attachment, and the background task
        removes it once the body has been sent. ``no-store`` because the file
        carries the session secret, every secret hash and any Home Assistant
        token: it is a credential, and a proxy or a browser cache has no
        business keeping a copy of it.
        """
        hub: Hub = app.state.hub
        target = backup_temp_path(env.data_dir)
        await run_in_threadpool(hub.db.backup_to, target)
        filename = backup_filename(datetime.now(dt_timezone.utc))
        log(logger, logging.INFO, "backup written", file=filename)
        return FileResponse(
            target,
            media_type=BACKUP_MEDIA_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(_delete_quietly, target),
        )

    @app.post("/settings/restore", dependencies=[Depends(require_admin)])
    async def post_settings_restore(request: Request) -> Response:
        """Replace the hub's database with an uploaded backup.

        The upload lands in DATA_DIR under its own name and is validated
        there (``app/backup.py``), so a file this build cannot restore is
        refused with the live database still in place and untouched: the
        answer is the settings page again, 422, with the reason on it.

        A file that passes is swapped in under ``Hub.identity_lock``, in the
        order ``Database.replace_file`` documents: close the connection,
        unlink the WAL and shm sidecars, ``os.replace``, reopen, migrate.
        Closing first is not tidiness - on Windows ``os.replace`` over a file
        with an open sqlite connection fails outright - and it is why a
        restore cannot be a copy over the live file. Then ``Hub.reload()``
        rebuilds the identity from the restored ``hub`` row, and the response
        sends the browser to /login with its cookie deleted: the session
        secret is the backup's now, so every cookie this hub ever signed,
        including the admin's own, is dead.
        """
        hub: Hub = app.state.hub
        _cap_restore_length(request)
        previous = hub.identity.config
        previous_device_key = "" if previous is None else previous.device_key_sha256
        try:
            async with request.form() as form:
                upload = form.get("file")
                if not isinstance(upload, UploadFile) or not upload.filename:
                    return _settings_html(
                        hub, error="Choose a backup file to restore.", status_code=422
                    )
                if not str(form.get("confirm", "")).strip():
                    return _settings_html(
                        hub,
                        error="Tick the confirmation box: a restore replaces this hub's "
                        "database, secrets and all.",
                        status_code=422,
                    )
                temp = restore_temp_path(env.data_dir)
                try:
                    size = await _stream_upload_to(upload, temp)
                    facts = await run_in_threadpool(inspect_backup, temp)
                except RestoreRejected as exc:
                    temp.unlink(missing_ok=True)
                    log(logger, logging.WARNING, "restore refused", reason=str(exc))
                    return _settings_html(hub, error=str(exc), status_code=422)
                except BaseException:
                    temp.unlink(missing_ok=True)
                    raise
        except MultiPartException as exc:
            raise HTTPException(status_code=413, detail="the uploaded file is too large") from exc

        async with hub.identity_lock:
            await run_in_threadpool(hub.db.replace_file, temp)
            await hub.reload()
        device_key_changed = facts.device_key_sha256 != previous_device_key
        log(logger, logging.WARNING, "database restored", bytes=size, schema=facts.schema_version)
        if device_key_changed:
            log(
                logger,
                logging.WARNING,
                "the restored backup carries a different device key: the flashed device "
                "stops fetching until its hub key is set to the one from this backup",
            )
        # The query is what login.html turns into a notice: the two hashes are
        # only knowable after the upload, so the warning cannot sit on the
        # confirmation form with the other two.
        location = "/login?restored=1" + ("&device_key_changed=1" if device_key_changed else "")
        response = RedirectResponse(location, status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.post("/settings/rotate", dependencies=[Depends(require_admin)])
    async def post_settings_rotate(request: Request) -> Response:
        """Mint a new token, device key and session secret, shown once.

        The old token and device key stop verifying as soon as the row is
        written, and the new session secret kills every cookie this hub ever
        signed. The response therefore carries a fresh admin cookie minted
        with the new secret: it replaces the dead one under the same name and
        path (which is how a cookie is deleted), so the admin reading the two
        secrets off this page is not locked out of the page they are on.
        """
        hub: Hub = app.state.hub
        await _cap_form_body(request)
        form = await request.form()
        if not str(form.get("confirm", "")).strip():
            return _settings_html(
                hub,
                error="Tick the confirmation box: rotating replaces both secrets and "
                "signs everyone out.",
                status_code=422,
            )
        config = hub.identity.config
        assert config is not None
        async with hub.identity_lock:
            replacement, token, device_key = rotate_secrets(config)
            await run_in_threadpool(write_hub_config, hub.db, replacement)
            await hub.reload()
        log(logger, logging.WARNING, "hub secrets rotated", name=replacement.name)
        template = hub.renderer.environment.get_template("rotated.html")
        html = template.render(
            name=replacement.name,
            base_url=replacement.base_url,
            token=token,
            device_key=device_key,
        )
        # Both secrets, shown once: never cache or store this page.
        response = HTMLResponse(
            html, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )
        response.set_cookie(
            COOKIE_NAME,
            mint_session_cookie(replacement.session_secret, time.time(), "admin"),
            max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
            secure=replacement.base_url.startswith("https"),
        )
        return response

    @app.post("/settings/{section}", dependencies=[Depends(require_admin)])
    async def post_settings_section(section: str, request: Request) -> Response:
        """Save one settings section, then reload the hub.

        Declared after /settings/backup, /settings/restore and
        /settings/rotate: routes match in declaration order, so the three
        literal paths have to be registered before this one can swallow them.

        A good submission redirects (303) back to the section it came from,
        which is what stops a reload of the page from re-posting it. A bad
        one comes back as the same page, 422, with each message against the
        input that caused it. "Save and test" saves the same way and then
        runs one forced fetch of the section's adapter, so the answer to "is
        this ICS URL right" is on the page rather than on the next render.
        """
        hub: Hub = app.state.hub
        if section not in SECTIONS:
            raise HTTPException(status_code=404, detail=f"unknown settings section {section}")
        await _cap_form_body(request)
        form = await request.form()
        refused = await _save_section(hub, section, form, search_url="/settings/geocode")
        if refused is not None:
            return _settings_html(
                hub, forms=_settings_forms(hub, replace=refused), status_code=422
            )
        if str(form.get("action", "")) == "test" and section in TESTABLE_SECTIONS:
            tested = await _tested_section_form(hub, section, search_url="/settings/geocode")
            return _settings_html(hub, forms=_settings_forms(hub, replace=tested))
        return RedirectResponse(f"/settings?saved={section}#{section}", status_code=303)

    # -- setup wizard ------------------------------------------------------
    @app.get(
        "/setup/{step}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_html)],
    )
    async def get_setup_step(step: str, request: Request) -> HTMLResponse:
        """One wizard step: that section's form and nothing else.

        ``require_admin_html`` is what makes an unconfigured hub send a
        browser back to /setup (SetupRedirect) and an unauthenticated or
        reader browser to /login: the wizard edits the same settings the
        settings page does and is guarded exactly like it.
        """
        hub: Hub = app.state.hub
        if step not in WIZARD_STEPS:
            raise HTTPException(status_code=404, detail=f"unknown setup step {step}")
        search = None
        if step == "weather":
            search = await _place_search(hub, request.query_params.get("q", ""), "/setup/weather")
        form = _section_form(hub, step, search=search, search_url="/setup/weather")
        return _wizard_html(hub, form, step)

    @app.post("/setup/{step}", dependencies=[Depends(require_admin)])
    async def post_setup_step(step: str, request: Request) -> Response:
        """Save a wizard step and move to the next one.

        The save is the settings page's save: same parser, same validation,
        same row, same reload. Only where it goes afterwards differs, and
        "Save and test" stays on the step so the result can be read.
        """
        hub: Hub = app.state.hub
        if step not in WIZARD_STEPS:
            raise HTTPException(status_code=404, detail=f"unknown setup step {step}")
        await _cap_form_body(request)
        form = await request.form()
        refused = await _save_section(hub, step, form, search_url="/setup/weather")
        if refused is not None:
            return _wizard_html(hub, refused, step, status_code=422)
        if str(form.get("action", "")) == "test" and step in TESTABLE_SECTIONS:
            tested = await _tested_section_form(hub, step, search_url="/setup/weather")
            return _wizard_html(hub, tested, step)
        return RedirectResponse(_wizard_next(step), status_code=303)

    # -- preview ---------------------------------------------------------
    @app.get("/")
    async def root() -> RedirectResponse:
        hub: Hub = app.state.hub
        if not hub.identity.configured:
            return RedirectResponse("/setup", status_code=303)
        return RedirectResponse("/preview")

    @app.get("/preview", response_class=HTMLResponse, dependencies=[Depends(require_reader_html)])
    async def preview(request: Request) -> HTMLResponse:
        hub: Hub = app.state.hub
        pages = hub.renderer.pages
        page = request.query_params.get("page", pages[0])
        if page not in pages:
            page = pages[0]
        state = await hub.state()
        template = hub.renderer.environment.get_template("preview.html")
        html = template.render(
            page=page,
            pages=list(pages),
            timezone=hub.hub_settings.general.timezone,
            updated_label=state.updated_at.strftime("%H:%M"),
            adapters={name: block.status.value for name, block in state.blocks.items()},
            cache_bust=request.query_params.get("t", int(time.time())),
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get(
        "/preview/{page}.html",
        response_class=HTMLResponse,
        dependencies=[Depends(require_reader_html)],
    )
    async def preview_page(page: str, request: Request) -> Response:
        hub: Hub = app.state.hub
        if not hub.has_page(page):
            return JSONResponse({"error": f"unknown page {page}"}, status_code=404)
        state = await hub.state(force="t" in request.query_params)
        html = hub.renderer.render_html(page, state, embed_fonts=False)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.exception_handler(LoginRedirect)
    async def login_redirect_handler(request: Request, exc: LoginRedirect) -> RedirectResponse:
        # safe="/": the path's own slashes must survive quoting unescaped,
        # or "/login?next=/preview" would come out as "...next=%2Fpreview".
        return RedirectResponse(f"/login?next={quote(exc.next_path, safe='/')}", status_code=303)

    @app.exception_handler(SetupRedirect)
    async def setup_redirect_handler(request: Request, exc: SetupRedirect) -> RedirectResponse:
        return RedirectResponse("/setup", status_code=303)

    return app


app = create_app()


def main() -> None:  # pragma: no cover - convenience entry point
    import uvicorn

    env = Env()
    # workers=1 (the default here): HubIdentity.claim()'s asyncio.Lock only
    # serializes concurrent POST /setup within one process (see Dockerfile).
    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=DEFAULT_PORT, log_level=env.log_level.lower()
    )


if __name__ == "__main__":  # pragma: no cover
    main()
