"""``POST /api/tasks``: the task list an agent pushes.

Core mounts this router under ``/api`` with ``Depends(require_token)``
applied (``app/main.py:create_app``), so the route itself never checks a
credential. Everything else it needs comes off the hub on
``request.app.state.hub``: the database to write the row, the cached adapter
to invalidate so the next state build sees it, and the section's source for
the warning when the panel is not actually reading pushes.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.datasets import push_warning, write_dataset
from app.models import TasksPush
from app.modules import ModuleContext
from app.timeutil import to_local

DATASET = "tasks"


def build_router(context: ModuleContext) -> APIRouter:
    router = APIRouter()

    @router.post("/tasks")
    async def post_tasks(payload: TasksPush, request: Request) -> JSONResponse:
        hub = request.app.state.hub
        received_at = datetime.now(dt_timezone.utc)
        document = {"tasks": [item.model_dump(mode="json") for item in payload.tasks]}
        await run_in_threadpool(write_dataset, hub.db, DATASET, document, received_at)
        # ``None`` when this module is installed but disabled: the row is
        # still written (turning a module off must not lose an agent's push)
        # and nothing draws it, which is what the warning below says.
        adapter = hub.state_service.adapters.get(DATASET)
        if adapter is not None:
            adapter.invalidate()
        configured = hub.hub_settings.tasks.source
        body: dict[str, Any] = {
            "stored": DATASET,
            "received_at": to_local(received_at, hub.hub_settings.general.timezone).isoformat(),
            "count": len(payload.tasks),
            "source": configured,
        }
        warning = push_warning(DATASET, configured)
        if warning is not None:
            body["warning"] = warning
        return JSONResponse(body)

    return router


__all__ = ["DATASET", "build_router"]
