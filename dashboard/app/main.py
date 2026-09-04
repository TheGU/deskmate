"""dashboard-hub FastAPI application.

Endpoints follow docs/ARCHITECTURE.md::

    GET    /healthz
    GET    /api/state
    GET    /display/{page}.png
    GET    /preview
    GET    /preview/{page}.html
    POST   /api/alert
    DELETE /api/alert
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.alerts import AlertStore
from app.config import Settings, get_settings
from app.logging_setup import configure_logging, log
from app.models import AlertRequest, DashboardState
from app.renderer.render import PAGE_TTL_SECONDS, PAGES, Renderer
from app.state import StateService, state_fingerprint

logger = logging.getLogger("app.main")


@dataclass(slots=True)
class RenderCacheEntry:
    fingerprint: str
    png: bytes
    etag: str
    created_monotonic: float


class Hub:
    """Everything the request handlers need, built once at startup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.alerts = AlertStore(settings.alert_file, settings.timezone)
        self.state_service = StateService(settings, self.alerts)
        self.renderer = Renderer(settings)
        self._cache: dict[str, RenderCacheEntry] = {}
        self._cache_lock = asyncio.Lock()

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
        )
        await hub.renderer.start()
        try:
            yield
        finally:
            await hub.renderer.close()
            log(logger, logging.INFO, "dashboard-hub stopped")

    app = FastAPI(title="dashboard-hub", version=__version__, lifespan=lifespan)
    app.state.hub = Hub(settings)
    if settings.static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

    # -- health ----------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        hub: Hub = app.state.hub
        state = await hub.state()
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "timezone": settings.timezone,
                "pages": list(PAGES),
                "adapters": {
                    name: {
                        "status": block.status.value,
                        "source": block.source,
                        "updated_at": block.updated_at.isoformat()
                        if block.updated_at
                        else None,
                        "error": block.error,
                    }
                    for name, block in state.blocks.items()
                },
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

    # -- alerts ----------------------------------------------------------
    @app.post("/api/alert")
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

    @app.delete("/api/alert")
    async def delete_alert() -> JSONResponse:
        hub: Hub = app.state.hub
        cleared = hub.alerts.clear()
        return JSONResponse({"cleared": cleared})

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

    # -- preview ---------------------------------------------------------
    @app.get("/")
    async def root() -> RedirectResponse:
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

    return app


app = create_app()


def main() -> None:  # pragma: no cover - convenience entry point
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, log_level=settings.log_level.lower())


if __name__ == "__main__":  # pragma: no cover
    main()
