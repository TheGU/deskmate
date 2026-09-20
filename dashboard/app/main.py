"""dashboard-hub FastAPI application.

Endpoints follow docs/ARCHITECTURE.md::

    GET    /healthz
    GET    /setup
    POST   /setup
    GET    /login
    POST   /login
    GET    /api/hub
    GET    /api/state
    GET    /display/{page}.png
    GET    /preview
    GET    /preview/{page}.html
    POST   /api/alert
    DELETE /api/alert
    POST   /api/device/telemetry
    GET    /api/device/telemetry
    GET    /api/device/history

Every installed module's push routes (``POST /api/<name>``, e.g. ai-usage,
brief, tasks) are mounted here at startup from ``app/modules/<id>/routes.py``
(see the registry loop in :func:`create_app`), and the settings page, the
setup wizard, and backup/restore/rotate (``GET/POST /settings...``,
``GET/POST /setup/{step}``) are registered by ``app/settings_pages.py``.

There is no module-level ``app`` object: uvicorn builds one through
:func:`create_app` itself (``--factory app.main:create_app``, see
:func:`main` and ``dashboard/Dockerfile``'s ``CMD``). A module-level
``app = create_app()`` used to sit at the bottom of this file, which meant
every import of ``app.main`` - including under pytest, before any test
asked for a hub at all - built a real :class:`Hub` and created
``data/deskmate.sqlite`` in the current directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from app import __version__
from app.adapters.device import HISTORY_MAX_POINTS, device_status, downsample
from app.alerts import AlertStore
from app.config import Env
from app.db import get_database
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
    require_device,
    require_reader,
    require_reader_html,
    require_token,
    validate_base_url,
)
from app.httputil import _cap_form_body, _read_capped_body
from app.legacy import LegacyEnv, import_legacy
from app.logging_setup import configure_logging, log
from app.models import (
    AlertRequest,
    DashboardState,
    DeviceSample,
    DeviceTelemetry,
)
from app.modules import Module, ModuleContext, ModuleError
from app.modules.registry import Registry, load_registry
from app.renderer.render import Renderer
from app.settings import HubSettings, SettingsStore, sections_for
from app.settings_pages import register as register_settings_pages
from app.state import StateService, state_fingerprint
from app.telemetry import TelemetryStore, TelemetrySummary, utc_now
from app.timeutil import to_local

logger = logging.getLogger("app.main")

#: Matches uvicorn.run's own port in ``main()`` below; there is no
#: configurable bind host/port setting today.
DEFAULT_PORT = 8080

#: Cap on the two telemetry-origin strings (main.py:post_device_telemetry,
#: telemetry.py's remote_addr/hub_host columns): plenty for an IPv6 address
#: or a "https://host:port" base URL, short enough that a hostile Host
#: header cannot grow the row without bound.
TELEMETRY_ORIGIN_MAX_LEN = 200

#: What ``/display/{page}.png`` reads as a page index rather than a page id.
#: ASCII digits only (``str.isdigit`` would also accept Arabic-Indic digits,
#: which ``int()`` parses and no page id could ever be), and bounded so a
#: pathological segment never reaches ``int()`` at all. A module id can never
#: collide with this: ``MODULE_ID_RE`` demands a leading letter and
#: ``validate_module`` refuses an all-digit id besides.
PAGE_INDEX_RE = re.compile(r"^[0-9]{1,9}$")


def resolve_page(registry: Registry, segment: str, known: bool) -> str | None:
    """The module id ``segment`` names, or ``None`` when nothing serves it.

    The device walks the pages by number, so ``/display/2.png`` is the third
    enabled page. Resolving here, before the render cache is consulted,
    is what keeps the cache key and the ``X-Deskmate-Page`` header the page's
    id: two devices sitting on different indices of the same page must share
    one cached PNG, and the telemetry the device posts back names the page it
    was told it is showing.

    ``known`` is the caller's own "this hub serves that id" answer
    (``Hub.has_page``), which covers ``alert`` as well as the module pages.
    ``alert`` is never an index: it is not in the registry's page list, and
    it is not digits.
    """
    if PAGE_INDEX_RE.match(segment):
        module = registry.page_by_index(int(segment))
        return None if module is None else module.id
    return segment if known else None


def resolve_telemetry_page(telemetry: DeviceTelemetry, registry: Registry) -> DeviceTelemetry:
    """``telemetry`` with ``page`` filled in from ``page_index`` if it has to.

    ``page`` is authoritative: the device sends the id it was told, and
    ``alert`` while an alert is showing. It is ``null`` only in a session
    that has not had a telemetry response yet, so the device knows which
    slot it is on but not what that slot is called; that is what
    ``page_index`` is for. An index the registry cannot resolve (the module
    was disabled since, or this is a device with a stale page count) stores
    ``null`` rather than a guess: the ``page`` column is what the System page
    draws, and a wrong id there would be invented data.
    """
    if telemetry.page is not None or telemetry.page_index is None:
        return telemetry
    module = registry.page_by_index(telemetry.page_index)
    if module is None:
        return telemetry
    return telemetry.model_copy(update={"page": module.id})


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

    Which sections these are is the store's own map, so seeding through the
    bootstrap store writes the built-ins and seeding through the real one
    adds whatever an installed module brought. A seed with nothing to say
    about a module's section writes that section's defaults, exactly as it
    does for a built-in it left alone.
    """
    for section, model in store.sections.items():
        if store.updated_at(section) is None:
            store.save(section, seed.section(section, model))


@dataclass(slots=True)
class _RebuiltSettings:
    """What :meth:`Hub._rebuild` computes, handed back rather than assigned
    onto ``self`` from inside it.

    ``_rebuild`` runs in a threadpool (``reload`` awaits it): if it assigned
    ``self.registry``/``self.sections``/``self.settings_store``/
    ``self.hub_settings`` itself, a request running concurrently on the event
    loop could read the *new* registry off ``self`` while ``self.renderer``
    and ``self.state_service`` - only rebuilt after the threadpool call
    returns - were still built from the *old* one (a just-enabled page's
    dataset missing from the old state service is a ``KeyError``, a 500).
    Returning the four together is what lets the caller publish them in one
    synchronous block instead.
    """

    registry: Registry
    sections: dict[str, type[BaseModel]]
    settings_store: SettingsStore
    hub_settings: HubSettings


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
        # Registry, section map, settings store and snapshot, in that order
        # and for that reason (see :meth:`_rebuild`). Every one of them is
        # rebuilt on reload, which is how a settings save takes effect.
        rebuilt = self._rebuild(hub_settings)
        self.registry = rebuilt.registry
        self.sections = rebuilt.sections
        self.settings_store = rebuilt.settings_store
        self.hub_settings = rebuilt.hub_settings
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

        ``_rebuild`` runs in a threadpool and only *returns* the new
        registry/sections/settings_store/hub_settings rather than assigning
        them onto ``self`` itself, and everything below that publishes them
        - the four assignments, the telemetry store, the state service, and
        the renderer's own two attributes - is one synchronous stretch with
        no ``await`` in it. A concurrent request reads ``self`` from the
        event loop, never mid-threadpool-call, so the only two states it can
        ever observe are "every one of these is still the old snapshot" or
        "every one of these is the new snapshot together"; a just-enabled
        page's dataset missing from an old state service paired with a new
        registry (a ``KeyError``, a 500) is the inconsistent state this
        forecloses.
        """
        identity = await run_in_threadpool(HubIdentity, self.db)
        self.identity = identity
        await run_in_threadpool(self.alerts.load)
        rebuilt = await run_in_threadpool(self._rebuild)
        self.registry = rebuilt.registry
        self.sections = rebuilt.sections
        self.settings_store = rebuilt.settings_store
        self.hub_settings = rebuilt.hub_settings
        self.alerts.set_timezone(self.hub_settings.general.timezone)
        # TelemetryStore owns no connection of its own (see __init__), but it
        # does carry retention_days as a plain attribute read by the
        # /api/device/telemetry summary: without rebuilding it here, a saved
        # device.retention_days would never reach that response.
        self.telemetry = TelemetryStore(self.db, self.hub_settings.device.retention_days)
        self.state_service = StateService(
            self.hub_settings, self.env, self.alerts, self.registry, self.db
        )
        self.renderer.hub_settings = self.hub_settings
        self.renderer.registry = self.registry
        async with self._cache_lock:
            self._cache.clear()
        log(logger, logging.INFO, "hub reloaded", configured=self.identity.configured)

    def _rebuild(self, seed: HubSettings | None = None) -> _RebuiltSettings:
        """Compute the registry, the section map, the store and the snapshot.

        The order is forced and looks circular until you follow it: the
        registry decides which settings sections exist, and the ``modules``
        section decides which modules the registry has. ``modules`` is a
        *core* section, so a store over the built-in map can always read it
        even on a hub whose real section map is not known yet. That is step
        one; everything else follows from the registry it builds.

        1. a bootstrap store over the built-ins, which is enough to read
           ``modules`` (and, for a caller that handed one in, to seed the
           built-in sections before that read, so a seeded ``modules``
           section is the one this registry is built from);
        2. the registry: built-ins, then ``deskmate.modules`` entry points,
           then the packages under ``DATA_DIR/modules/``. A duplicate or
           invalid id refuses to load rather than being dropped, so a hub
           either serves what the settings page says it serves or does not
           come up;
        3. the section map, which is every installed module's own section
           (enabled or not) plus the core ones;
        4. the real store over that map, seeded again for whatever the
           bootstrap store had no section for, and its snapshot.

        Returns the four instead of assigning them onto ``self``: this runs
        in a threadpool from :meth:`reload`, on a database that never
        changes underneath it mid-call, but ``self`` is read concurrently
        from the event loop the whole time. Assigning here, one at a time,
        would let a request see a new registry paired with an old state
        service and renderer (see :meth:`reload`'s own docstring); the
        caller publishes all four together instead, synchronously, with
        nothing in between them for a request to land in.

        Plain synchronous I/O throughout, like the store itself:
        :meth:`reload` calls it in a threadpool.
        """
        bootstrap = SettingsStore(self.db)
        if seed is not None:
            _seed_missing_sections(bootstrap, seed)
        registry = load_registry(bootstrap.modules(), self.env.data_dir)
        sections = sections_for(registry)
        settings_store = SettingsStore(self.db, sections)
        if seed is not None:
            _seed_missing_sections(settings_store, seed)
        hub_settings = settings_store.snapshot()
        return _RebuiltSettings(
            registry=registry,
            sections=sections,
            settings_store=settings_store,
            hub_settings=hub_settings,
        )

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
    # page_index is a request-only field: the hub resolves it to an id on the
    # way in (:func:`resolve_telemetry_page`) and the telemetry table has no
    # column for it, so echoing a permanent null back on every read would only
    # invite a client to believe it.
    payload = sample.model_dump(mode="json", exclude={"page_index"})
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


def _route_signatures(app: FastAPI) -> set[tuple[str, str]]:
    """Every ``(method, path)`` pair a route already mounted on ``app``
    answers for.

    What the module-router collision check compares a new router's routes
    against: recomputed fresh each time a module is mounted (see
    ``create_app``), so it also catches two modules claiming the same path,
    not only a module against core.
    """
    signatures: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        signatures.update((method, path) for method in methods)
    return signatures


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

    # The settings page, the setup wizard, and backup/restore/rotate
    # (app/settings_pages.py) close over this same env, the way every route
    # left in this module does.
    register_settings_pages(app, env)

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

        telemetry = resolve_telemetry_page(telemetry, hub.registry)
        remote_addr, hub_host = _telemetry_origin(request)
        received_at = await run_in_threadpool(
            hub.telemetry.insert,
            telemetry,
            remote_addr=remote_addr,
            hub_host=hub_host,
        )
        # The next page render must see this sample, not the cached one.
        # ``get``, not ``[]``: the device dataset belongs to the system
        # module, and a hub with that module disabled still takes telemetry.
        device_adapter = hub.state_service.adapters.get("device")
        if device_adapter is not None:
            device_adapter.invalidate()
        # page_count and pages are what firmware/e1002.yaml learns the window
        # list from (it parses this very response, see the on_response lambda
        # by its http_request.post): the device holds no page list of its own,
        # so enabling, disabling or reordering a module here reaches it on its
        # next post with no reflash. ``alert`` is never in the list.
        page_ids = hub.registry.page_ids()
        # refresh_minutes/telemetry_minutes/wake_hours: the device's refresh
        # schedule, moved out of firmware/e1002.yaml's compiled substitutions
        # (docs/plan finding 11) and into this section, so a saved change
        # reaches the device on its very next post with no reflash. The
        # on_response lambda in firmware/e1002.yaml parses these three
        # alongside page_count/pages.
        device_settings = hub.hub_settings.device
        return JSONResponse(
            {
                "accepted": True,
                "received_at": to_local(received_at, hub.hub_settings.general.timezone).isoformat(),
                "page_count": len(page_ids),
                "pages": list(page_ids),
                "refresh_minutes": device_settings.refresh_minutes,
                "telemetry_minutes": device_settings.telemetry_minutes,
                "wake_hours": device_settings.wake_hours_list,
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
        """One page as a PNG, by id (``today``) or by index (``0``).

        The index is resolved to the id first (:func:`resolve_page`), so
        everything past this line - the render cache key, the ETag, the
        ``X-Deskmate-Page`` header and the log line - is the id, whichever
        form the device asked with.
        """
        hub: Hub = app.state.hub
        resolved = resolve_page(hub.registry, page, hub.has_page(page))
        if resolved is None:
            return JSONResponse({"error": f"unknown page {page}"}, status_code=404)
        page = resolved
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

    # -- pushed data -------------------------------------------------------
    # A module that declares routes hands core an APIRouter; core mounts it
    # under /api with the bearer-token dependency applied, so a module
    # cannot forget auth. Each push route writes its dataset row in a
    # threadpool and invalidates the matching CachedAdapter so /api/state
    # reflects it on the very next build, not after the adapter's own TTL
    # (app/modules/<id>/routes.py).
    #
    # Mounted last, after every one of core's own routes above: Starlette
    # matches routes in declaration order, so a module router included
    # before /api/alert or /api/device/telemetry could shadow either of
    # them - the module's own handler would run instead of core's, silently,
    # for as long as the process stayed up. Declaring core's routes first
    # and refusing a collision below (rather than only ordering around it)
    # is what makes that impossible instead of merely unlikely.
    #
    # Every installed module, not only the enabled ones: routes are fixed at
    # startup (FastAPI has no unmount), so mounting only the enabled ones
    # would mean a restart after every enable, while leaving a disabled
    # module's route up costs nothing - it writes a row nothing draws and
    # says exactly that in its own "warning" field.
    for module in app.state.hub.registry.modules:
        if module.routes is None:
            continue
        router = module.routes(app.state.hub.module_context(module))
        prefix = "/api"
        existing = _route_signatures(app)
        for route in router.routes:
            full_path = prefix + getattr(route, "path", "")
            for method in getattr(route, "methods", None) or ():
                if (method, full_path) in existing:
                    raise ModuleError(
                        f"module {module.id!r}: route {method} {full_path} collides "
                        "with a core path; rename it"
                    )
        app.include_router(router, prefix=prefix, dependencies=[Depends(require_token)])

    @app.exception_handler(LoginRedirect)
    async def login_redirect_handler(request: Request, exc: LoginRedirect) -> RedirectResponse:
        # safe="/": the path's own slashes must survive quoting unescaped,
        # or "/login?next=/preview" would come out as "...next=%2Fpreview".
        return RedirectResponse(f"/login?next={quote(exc.next_path, safe='/')}", status_code=303)

    @app.exception_handler(SetupRedirect)
    async def setup_redirect_handler(request: Request, exc: SetupRedirect) -> RedirectResponse:
        return RedirectResponse("/setup", status_code=303)

    return app


def main() -> None:  # pragma: no cover - convenience entry point
    import uvicorn

    env = Env()
    # workers=1 (the default here): HubIdentity.claim()'s asyncio.Lock only
    # serializes concurrent POST /setup within one process (see Dockerfile).
    # factory=True: create_app() is called by uvicorn itself, once, inside
    # its own process, rather than at import time (see the module docstring
    # for why a module-level ``app = create_app()`` was removed).
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=DEFAULT_PORT,
        log_level=env.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
