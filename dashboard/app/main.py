"""dashboard-hub FastAPI application.

Endpoints follow docs/ARCHITECTURE.md::

    GET    /healthz
    GET    /setup
    POST   /setup
    GET    /api/hub
    GET    /api/state
    POST   /api/ai-usage
    POST   /api/brief
    POST   /api/tasks
    GET    /display/{page}.png
    GET    /preview
    GET    /preview/{page}.html
    GET    /preview/{page}-rgb.png
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

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app import __version__
from app.adapters.ai_brief import current_mode
from app.adapters.device import HISTORY_MAX_POINTS, device_status, downsample
from app.alerts import AlertStore
from app.config import Settings, get_settings
from app.hub_config import (
    AlreadyConfigured,
    HubIdentity,
    InvalidBaseURL,
    WrongClaimCode,
    require_token,
)
from app.logging_setup import configure_logging, log
from app.models import (
    AIUsagePush,
    AlertRequest,
    BriefPush,
    DashboardState,
    DeviceSample,
    DeviceTelemetry,
    TasksPush,
)
from app.renderer.palette import to_png_bytes
from app.renderer.render import PAGE_TTL_SECONDS, PAGES, Renderer
from app.state import StateService, state_fingerprint
from app.telemetry import TelemetrySummary, get_telemetry_store, utc_now
from app.timeutil import to_local

logger = logging.getLogger("app.main")

#: Matches uvicorn.run's own port in ``main()`` below; there is no
#: configurable bind host/port setting today, so the claim-code log line
#: always names 0.0.0.0 with a note to use the LAN address instead.
DEFAULT_PORT = 8080

#: /api/device/telemetry and /setup are the only POST routes with no token
#: (see the trade-off note on the telemetry route below), so anyone on the
#: LAN can point either one at this single-worker container. Cap what either
#: route will buffer in memory before validation ever runs.
MAX_OPEN_BODY_BYTES = 64 * 1024


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


class Hub:
    """Everything the request handlers need, built once at startup."""

    def __init__(self, settings: Settings) -> None:
        _require_writable_data_dir(settings.data_dir)
        self.settings = settings
        self.alerts = AlertStore(settings.alert_file, settings.timezone)
        self.telemetry = get_telemetry_store(settings)
        self.state_service = StateService(settings, self.alerts)
        self.renderer = Renderer(settings)
        self.identity = HubIdentity(settings.hub_config_file)
        self._cache: dict[str, RenderCacheEntry] = {}
        self._cache_lock = asyncio.Lock()
        if not self.identity.configured:
            log(
                logger,
                logging.WARNING,
                f"Setup needed: open http://0.0.0.0:{DEFAULT_PORT}/setup and enter "
                f"claim code {self.identity.claim_code}",
                note="0.0.0.0 is not reachable from another device; use this machine's LAN address",
            )

    async def state(self, *, force: bool = False) -> DashboardState:
        return await self.state_service.build(force=force)

    async def png(self, page: str, state: DashboardState, *, force: bool) -> RenderCacheEntry:
        """Return the cached PNG for ``page`` or render a fresh one."""
        fingerprint = state_fingerprint(state)
        ttl = PAGE_TTL_SECONDS.get(page, 0.0)
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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomic JSON write: fsync the temp file, then ``os.replace`` it into place.

    Same pattern as ``app/alerts.py`` and ``app/hub_config.py``, plus two
    things they do not need for a single small file written on every push:
    ``fsync`` before the rename, so a crash right after does not leave the
    rename pointing at a truncated file, and removing the temp file on any
    failure (a bad serializer, a full disk) instead of leaving a stray
    ``*.tmp`` in ``DATA_DIR``. Creates the parent directory when absent, so
    a first push into ``DATA_DIR/brief`` does not need it to exist already.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    tmp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


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


def _effective_source_after_push(configured: str) -> str:
    """What the panel will actually serve right after a successful push:
    values are "file", "fixture" or "obsidian" (tasks only). The push just
    wrote the file, so "file" and "auto" (which now sees the file) both
    become "file"; a selector pinned to "fixture" or "obsidian" is reported
    verbatim, since the push did not change what the panel shows (obsidian
    reads the vault regardless of any tasks.json).
    """
    return "file" if configured in ("file", "auto") else configured


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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub: Hub = app.state.hub
        log(
            logger,
            logging.INFO,
            "dashboard-hub starting",
            version=__version__,
            timezone=settings.timezone,
            fixtures=str(settings.fixtures_dir),
            tasks=settings.tasks_source,
            calendar=settings.calendar_source,
            weather=settings.weather_source,
            ai_usage=settings.ai_usage_source,
            brief=settings.brief_source,
            home=settings.ha_source,
            device=settings.device_source,
            telemetry_db=str(settings.telemetry_db_file),
        )
        await hub.renderer.start()
        try:
            yield
        finally:
            await hub.renderer.close()
            # The telemetry store is process-wide and may be shared with another
            # app instance (tests build several), so shutdown leaves it open.
            # Every insert commits, so nothing is lost when the process exits.
            log(logger, logging.INFO, "dashboard-hub stopped")

    app = FastAPI(title="dashboard-hub", version=__version__, lifespan=lifespan)
    app.state.hub = Hub(settings)
    if settings.static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

    # -- health ----------------------------------------------------------
    #: A healthz-only word for an adapter that has never fetched yet.
    #: Deliberately not an AdapterStatus member: calling that "error" or
    #: "unavailable" before anything has even tried would be a lie.
    HEALTH_STATUS_UNKNOWN = "unknown"

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness check: answers from each adapter's last known outcome
        and never fetches, so an outage cannot make this route slow (see the
        CachedAdapter failure backoff in adapters/base.py). GET /api/state
        is what forces every adapter to fetch and reports live status.
        """
        hub: Hub = app.state.hub
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
                "status": "ok",
                "version": __version__,
                "timezone": settings.timezone,
                "pages": list(PAGES),
                "renderer": {"connected": hub.renderer.connected},
                "adapters": adapters,
                "alert": hub.alerts.current.priority.value if hub.alerts.current else None,
            }
        )

    # -- state -----------------------------------------------------------
    @app.get("/api/state")
    async def api_state(request: Request) -> Response:
        hub: Hub = app.state.hub
        state = await hub.state(force="t" in request.query_params)
        return Response(
            content=state.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    # -- hub identity ------------------------------------------------------
    @app.get("/api/hub")
    async def api_hub() -> JSONResponse:
        hub: Hub = app.state.hub
        config = hub.identity.config
        return JSONResponse(
            {
                "name": config.name if config else None,
                "base_url": config.base_url if config else None,
                "configured": hub.identity.configured,
                "version": __version__,
                "timezone": settings.timezone,
                # "configured" is the settings value (may be "auto"); "effective"
                # is what the *next* render will use ("fixture", "file", or
                # "obsidian" for tasks) - a live check, not the last fetch, so
                # a push shows up here immediately. The footer's DEMO mark
                # instead tracks the last actual fetch (what is currently
                # drawn), which can lag until the next render.
                "sources": {
                    "ai_usage": {
                        "configured": settings.ai_usage_source,
                        "effective": hub.state_service.ai_usage.resolve(),
                    },
                    "brief": {
                        "configured": settings.brief_source,
                        "effective": hub.state_service.brief.resolve(),
                    },
                    "tasks": {
                        "configured": settings.tasks_source,
                        "effective": hub.state_service.tasks.resolve(),
                    },
                },
            }
        )

    # -- pushed data -------------------------------------------------------
    # Every push writes atomically in a threadpool, then invalidates the
    # matching CachedAdapter so /api/state reflects it on the very next
    # build, not after the adapter's own TTL.
    @app.post("/api/ai-usage", dependencies=[Depends(require_token)])
    async def post_ai_usage(payload: AIUsagePush) -> JSONResponse:
        hub: Hub = app.state.hub
        received_at = datetime.now(dt_timezone.utc)
        document = {
            "received_at": received_at.isoformat(),
            "providers": [item.model_dump(mode="json") for item in payload.providers],
        }
        await run_in_threadpool(_write_json_atomic, settings.ai_usage_file, document)
        hub.state_service.ai_usage.invalidate()
        effective = _effective_source_after_push(settings.ai_usage_source)
        body: dict[str, Any] = {
            "stored": settings.ai_usage_file.name,
            "received_at": to_local(received_at, settings.timezone).isoformat(),
            "count": len(payload.providers),
            "effective_source": effective,
        }
        if effective != "file":
            body["warning"] = (
                f"AI_USAGE_SOURCE is {settings.ai_usage_source}; the panel will not show this push"
            )
        return JSONResponse(body)

    @app.post("/api/brief", dependencies=[Depends(require_token)])
    async def post_brief(payload: BriefPush) -> JSONResponse:
        hub: Hub = app.state.hub
        received_at = datetime.now(dt_timezone.utc)
        mode = payload.mode or current_mode(settings)
        document = {
            "received_at": received_at.isoformat(),
            "mode": mode.value,
            "headline": payload.headline,
            "note": payload.note or "",
            "sections": [section.model_dump(mode="json") for section in payload.sections],
            "generated_at": payload.generated_at.isoformat(),
        }
        target = settings.brief_directory / "current.json"
        await run_in_threadpool(_write_json_atomic, target, document)
        hub.state_service.brief.invalidate()
        effective = _effective_source_after_push(settings.brief_source)
        body: dict[str, Any] = {
            "stored": target.name,
            "received_at": to_local(received_at, settings.timezone).isoformat(),
            "count": len(payload.sections),
            "effective_source": effective,
        }
        if effective != "file":
            body["warning"] = (
                f"BRIEF_SOURCE is {settings.brief_source}; the panel will not show this push"
            )
        return JSONResponse(body)

    @app.post("/api/tasks", dependencies=[Depends(require_token)])
    async def post_tasks(payload: TasksPush) -> JSONResponse:
        hub: Hub = app.state.hub
        received_at = datetime.now(dt_timezone.utc)
        document = {
            "received_at": received_at.isoformat(),
            "tasks": [item.model_dump(mode="json") for item in payload.tasks],
        }
        await run_in_threadpool(_write_json_atomic, settings.tasks_file, document)
        hub.state_service.tasks.invalidate()
        effective = _effective_source_after_push(settings.tasks_source)
        body: dict[str, Any] = {
            "stored": settings.tasks_file.name,
            "received_at": to_local(received_at, settings.timezone).isoformat(),
            "count": len(payload.tasks),
            "effective_source": effective,
        }
        if effective != "file":
            body["warning"] = (
                f"TASKS_SOURCE is {settings.tasks_source}; the panel will not show this push"
            )
        return JSONResponse(body)

    # -- alerts ------------------------------------------------------------
    @app.post("/api/alert", dependencies=[Depends(require_token)])
    async def post_alert(payload: AlertRequest) -> JSONResponse:
        hub: Hub = app.state.hub
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
    # Trade-off, stated in docs/DATA-SOURCES.md: this endpoint stays open
    # (no require_token) because the E1002 firmware does not send a bearer
    # token today. firmware/e1002.yaml:363-365 already sends request_headers
    # on this POST, so adding the token is a two-line firmware change and a
    # reflash; it is deferred by choice, tracked as a follow-up, not shipped
    # here.
    @app.post("/api/device/telemetry")
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

        received_at = await run_in_threadpool(hub.telemetry.insert, telemetry)
        # The next page render must see this sample, not the cached one.
        hub.state_service.device.invalidate()
        return JSONResponse(
            {
                "accepted": True,
                "received_at": to_local(received_at, settings.timezone).isoformat(),
            },
            status_code=202,
        )

    @app.get("/api/device/telemetry")
    async def get_device_telemetry() -> JSONResponse:
        hub: Hub = app.state.hub
        latest, summary = await run_in_threadpool(_read_latest, hub)
        age = None if latest is None else round((utc_now() - latest.received_at).total_seconds(), 1)
        return JSONResponse(
            {
                "status": device_status(age).value,
                "source": settings.device_source,
                "age_seconds": age,
                "latest": None if latest is None else _sample_json(latest, settings.timezone),
                "summary": {
                    "sample_count": summary.sample_count,
                    "oldest": _stamp(summary.oldest, settings.timezone),
                    "newest": _stamp(summary.newest, settings.timezone),
                    "retention_days": hub.telemetry.retention_days,
                },
            },
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/device/history")
    async def get_device_history(
        hours: float = Query(default=24.0, gt=0.0, le=8760.0),
    ) -> JSONResponse:
        hub: Hub = app.state.hub
        samples = await run_in_threadpool(hub.telemetry.history, hours)
        points = downsample(samples, HISTORY_MAX_POINTS)
        return JSONResponse(
            {
                "hours": hours,
                "sample_count": len(samples),
                "point_count": len(points),
                "max_points": HISTORY_MAX_POINTS,
                "downsampled": len(points) < len(samples),
                "samples": [_sample_json(sample, settings.timezone) for sample in points],
            },
            headers={"Cache-Control": "no-cache"},
        )

    # -- display ---------------------------------------------------------
    @app.get("/display/{page}.png")
    async def display(page: str, request: Request) -> Response:
        hub: Hub = app.state.hub
        if page not in PAGES:
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
            config = hub.identity.config
            assert config is not None
            template = hub.renderer.environment.get_template("setup-configured.html")
            html = template.render(
                name=config.name,
                base_url=config.base_url,
                created_at=to_local(config.created_at, settings.timezone).isoformat(),
            )
            return HTMLResponse(html)
        guessed_base_url = f"{request.url.scheme}://{request.headers.get('host', '')}"
        template = hub.renderer.environment.get_template("setup.html")
        html = template.render(default_name="deskmate", default_base_url=guessed_base_url)
        return HTMLResponse(html)

    @app.post("/setup", response_class=HTMLResponse)
    async def post_setup(request: Request) -> HTMLResponse:
        hub: Hub = app.state.hub
        if hub.identity.error is not None:
            raise HTTPException(status_code=503, detail=hub.identity.error)
        # request.form() reads request.stream() itself, so it cannot be handed
        # the capped helper's bytes directly. When Content-Length is present,
        # reject an oversized body before form() ever touches the stream; when
        # it is absent (e.g. chunked), read the capped body first so it is
        # cached on request._body, which stream() (and so form()) reuses.
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
        form = await request.form()
        name = str(form.get("name", "")).strip()
        base_url = str(form.get("base_url", ""))
        claim_code = str(form.get("claim_code", ""))
        # Capped before claim_hub's own base-URL validation runs, so an
        # absurd or empty name/base_url is a plain 422, not whatever urlsplit
        # or a downstream write does with it.
        if not name or len(name) > 60:
            raise HTTPException(status_code=422, detail="name must be 1 to 60 characters")
        if len(base_url) > 200:
            raise HTTPException(status_code=422, detail="base_url must be 200 characters or fewer")
        try:
            token = await hub.identity.claim(
                submitted_code=claim_code, name=name, base_url=base_url
            )
        except AlreadyConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WrongClaimCode as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except InvalidBaseURL as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config = hub.identity.config
        assert config is not None
        template = hub.renderer.environment.get_template("setup-done.html")
        html = template.render(
            name=config.name,
            base_url=config.base_url,
            token=token,
            skill_path="skills/deskmate/SKILL.md",
        )
        # This page carries the token, shown once: never cache or store it.
        return HTMLResponse(
            html, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )

    # -- preview ---------------------------------------------------------
    @app.get("/")
    async def root() -> RedirectResponse:
        hub: Hub = app.state.hub
        if not hub.identity.configured:
            return RedirectResponse("/setup")
        return RedirectResponse("/preview")

    @app.get("/preview", response_class=HTMLResponse)
    async def preview(request: Request) -> HTMLResponse:
        hub: Hub = app.state.hub
        page = request.query_params.get("page", "today")
        if page not in PAGES:
            page = "today"
        state = await hub.state()
        template = hub.renderer.environment.get_template("preview.html")
        html = template.render(
            page=page,
            pages=list(PAGES),
            timezone=settings.timezone,
            updated_label=state.updated_at.strftime("%H:%M"),
            adapters={name: block.status.value for name, block in state.blocks.items()},
            cache_bust=request.query_params.get("t", int(time.time())),
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/preview/{page}.html", response_class=HTMLResponse)
    async def preview_page(page: str, request: Request) -> Response:
        hub: Hub = app.state.hub
        if page not in PAGES:
            return JSONResponse({"error": f"unknown page {page}"}, status_code=404)
        state = await hub.state(force="t" in request.query_params)
        html = hub.renderer.render_html(page, state, embed_fonts=False)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/preview/{page}-rgb.png")
    async def preview_rgb(page: str, request: Request) -> Response:
        """The RGB stage before quantization, for the developer preview.

        Not cached: it exists to let a developer see what the six-ink snap
        removes, not to serve the panel.
        """
        hub: Hub = app.state.hub
        if page not in PAGES:
            return JSONResponse({"error": f"unknown page {page}"}, status_code=404)
        state = await hub.state(force="t" in request.query_params)
        image = await hub.renderer.render_rgb(page, state)
        payload = to_png_bytes(image)
        return Response(
            content=payload,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()


def main() -> None:  # pragma: no cover - convenience entry point
    import uvicorn

    settings = get_settings()
    # workers=1 (the default here): HubIdentity.claim()'s asyncio.Lock only
    # serializes concurrent POST /setup within one process (see Dockerfile).
    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=DEFAULT_PORT, log_level=settings.log_level.lower()
    )


if __name__ == "__main__":  # pragma: no cover
    main()
