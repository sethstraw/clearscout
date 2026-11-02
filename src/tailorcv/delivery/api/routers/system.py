"""System HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from tailorcv import __version__
from tailorcv.application.failures import UnavailableError
from tailorcv.application.tasks import TaskQueueService
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import (
    ApiState,
    ApiStateDep,
    api_state,
    database_for,
    paid_work_ready,
    schema_state,
)
from tailorcv.delivery.api.models.system import HealthResponse

router = APIRouter()


@router.get("/openapi.json", include_in_schema=False)
def openapi_contract(request: Request) -> JSONResponse:
    """Expose the generated contract before the frontend catch-all."""
    return JSONResponse(request.app.openapi())


@router.get(f"{API_PREFIX}/docs", include_in_schema=False)
def api_docs(request: Request) -> RedirectResponse:
    """Open the same-origin playground without remote Swagger assets."""
    destination = (
        "/api-playground"
        if api_state(request).frontend_directory is not None
        else f"{API_PREFIX}/openapi.json"
    )
    return RedirectResponse(url=destination, status_code=307)


@router.get(f"{API_PREFIX}/health", response_model=HealthResponse, tags=["system"])
def health(state: ApiStateDep) -> HealthResponse:
    """Report service and database migration health."""
    schema = schema_state(state)
    reachable = False
    worker_available = False
    if schema is not None and schema.database_exists:
        try:
            database = database_for(state)
            database.check()
            reachable = True
            if schema.current:
                worker_available = TaskQueueService(database).worker_available()
        except Exception:  # noqa: BLE001 - health reports failure without leaking internals
            reachable = False
    # Queued work cannot run without a worker, so the summary reports it.
    healthy = schema is not None and schema.current and reachable and worker_available
    return HealthResponse(
        status="ok" if healthy else "degraded",
        version=__version__,
        database_exists=schema.database_exists if schema is not None else False,
        database_reachable=reachable,
        schema_current=schema.current if schema is not None else False,
        current_revision=schema.current_revision if schema is not None else None,
        head_revision=schema.head_revision if schema is not None else None,
        worker_available=worker_available,
        free_mode=free_mode(state),
    )


def free_mode(state: ApiState) -> bool:
    """Report free mode, treating unreadable settings as no paid work possible."""
    try:
        return not paid_work_ready(state)
    except UnavailableError:
        return True
