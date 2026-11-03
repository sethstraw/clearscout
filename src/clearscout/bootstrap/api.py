"""Versioned local HTTP adapter for durable ClearScout application services."""

from pathlib import Path
from secrets import token_urlsafe

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette_admin import DropDown
from starlette_admin.contrib.sqla import Admin, ModelView
from starlette_admin.views import BaseView

from clearscout import __version__
from clearscout.application.config import Settings
from clearscout.delivery.api.constants import API_PREFIX as VERSIONED_API_PREFIX
from clearscout.delivery.api.dependencies import (
    ApiState as ApplicationState,
)
from clearscout.delivery.api.dependencies import (
    api_state as request_api_state,
)
from clearscout.delivery.api.dependencies import (
    lifespan as api_lifespan,
)
from clearscout.delivery.api.middleware import install_middleware, problem
from clearscout.delivery.api.models.system import ApiOnlyRootResponse
from clearscout.delivery.api.routers.billing import router as billing_router
from clearscout.delivery.api.routers.documents import router as documents_router
from clearscout.delivery.api.routers.jobs import router as jobs_router
from clearscout.delivery.api.routers.prompts import router as prompts_router
from clearscout.delivery.api.routers.providers import router as providers_router
from clearscout.delivery.api.routers.records import router as records_router
from clearscout.delivery.api.routers.search import router as search_router
from clearscout.delivery.api.routers.system import router as system_router
from clearscout.delivery.api.routers.tasks import router as tasks_router
from clearscout.delivery.api.routers.workspaces import router as workspaces_router
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Base
from clearscout.infrastructure.providers.credentials import KeyringStore, KeyStore

ADMIN_PREFIX = "/admin"
ADMIN_STATIC = Path(__file__).resolve().parent / "admin_static"
ADMIN_SCRIPT = f"{ADMIN_PREFIX}/static/clearscout-admin.js"

# The model modules already group the schema. Groups run from the job-search
# working set to the append-only record, and the named classes lead their group.
ADMIN_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "discovery",
        "Job search",
        ("JobPosting", "Employer", "Source", "SourceScan", "PendingDiscovery"),
    ),
    (
        "evaluations",
        "Evaluation",
        ("JobEvaluation", "EvaluationScore", "PostingRequirement", "Application"),
    ),
    (
        "tailoring",
        "Tailoring",
        ("TailoringRun", "ReviewProposal", "ApprovalDecision", "ModelCall", "CostRecord"),
    ),
    ("tasks", "Tasks", ("BackgroundTask", "BatchRun", "TaskEvent", "WorkerPresence")),
    (
        "workspaces",
        "Workspace setup",
        ("Workspace", "CandidateProfile", "MasterCvVersion", "JobSearchParameterSet"),
    ),
    ("records", "Notes and tags", ("Note", "Tag", "SavedView")),
    ("artifacts", "Audit and files", ("AuditEvent", "Artifact")),
)
ADMIN_FALLBACK_LABEL = "Other"


def create_app(
    database_url: str | None = None,
    artifact_root: Path | None = None,
    *,
    data_root: Path | None = None,
    workspaces_root: Path | None = None,
    read_only_import_roots: tuple[Path, ...] = (),
    key_store: KeyStore | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the API without creating a database or running migrations.

    The key store and settings are injected by tests. Left alone they are the
    real system keyring and the settings file the CLI reads, resolved per request
    so that editing either is visible without a restart.
    """
    storage = RuntimeStorage.resolve(
        database_url,
        data_root=data_root,
        artifact_root=artifact_root,
        workspaces_root=workspaces_root,
        read_only_import_roots=read_only_import_roots,
    )
    state = ApplicationState(storage, key_store or KeyringStore(), settings)
    app = FastAPI(
        title="ClearScout local API",
        version=__version__,
        description=(
            "Loopback-only API for ClearScout's durable evidence ledger, document workflows, "
            "and recoverable task queue. Contracts are generated from the same validated "
            "request and response fields used by the application."
        ),
        docs_url=None,
        openapi_url=f"{VERSIONED_API_PREFIX}/openapi.json",
        redoc_url=None,
        lifespan=api_lifespan,
    )
    app.state.clearscout = state
    install_middleware(app)

    for router in (
        system_router,
        providers_router,
        billing_router,
        workspaces_router,
        jobs_router,
        documents_router,
        prompts_router,
        records_router,
        search_router,
        tasks_router,
    ):
        app.include_router(router)

    # Registered before the SPA catch-all so a mistyped endpoint stays a 404.
    app.add_api_route(
        "/api/{unmatched:path}",
        unmatched_api_route,
        include_in_schema=False,
        methods=["GET", "HEAD"],
    )

    _mount_admin(app, state)

    frontend = _frontend_directory()
    state.frontend_directory = frontend
    if frontend is not None:
        app.mount(
            "/assets",
            StaticFiles(directory=frontend / "assets", check_dir=True),
            name="frontend-assets",
        )
        app.add_api_route(
            "/{frontend_path:path}",
            frontend_application,
            include_in_schema=False,
            methods=["GET", "HEAD"],
        )
    else:
        app.add_api_route("/", api_only_root, include_in_schema=False, methods=["GET"])
    return app


class ReadOnlyView(ModelView):
    """Model view that offers no create, edit, or delete on any table."""

    def can_create(self, request: Request) -> bool:
        """Refuse to offer a row insert."""
        del request
        return False

    def can_edit(self, request: Request) -> bool:
        """Refuse to offer a row update."""
        del request
        return False

    def can_delete(self, request: Request) -> bool:
        """Refuse to offer a row delete."""
        del request
        return False


def _admin_menu() -> list[DropDown]:
    """Build one view per mapped model, grouped into the menu sections above."""
    by_module: dict[str, dict[str, BaseView]] = {}
    for mapper in Base.registry.mappers:
        model = mapper.class_
        instance = ReadOnlyView(model)
        # Rendering appends field scripts to this list, so it must not be shared.
        instance.additional_js_links = [ADMIN_SCRIPT]
        by_module.setdefault(model.__module__.rsplit(".", 1)[-1], {})[model.__name__] = instance
    groups = []
    for module, label, lead in ADMIN_GROUPS:
        views = by_module.pop(module, {})
        ordered: list[BaseView | type[BaseView]] = [
            views.pop(name) for name in lead if name in views
        ]
        ordered.extend(views[name] for name in sorted(views))
        groups.append(DropDown(label, views=ordered))
    # A model whose module no group names is still reachable, rather than absent
    # from a menu that claims to list the schema.
    remaining = {name: view for views in by_module.values() for name, view in views.items()}
    if remaining:
        groups.append(
            DropDown(ADMIN_FALLBACK_LABEL, views=[remaining[name] for name in sorted(remaining)])
        )
    return groups


def _mount_admin(app: FastAPI, state: ApplicationState) -> None:
    """Mount the model admin ahead of the SPA catch-all that would swallow it."""
    database = Database(state.storage.database_url, require_existing=False)
    state.opened_database = database
    # The admin holds no session worth surviving a restart, so a per-process key is enough.
    admin = Admin(
        database.engine,
        title="ClearScout admin",
        base_url=ADMIN_PREFIX,
        secret_key=token_urlsafe(32),
        static_dir=str(ADMIN_STATIC),
    )
    for group in _admin_menu():
        admin.add_view(group)
    admin.mount_to(app)
    # The mount matches only "/admin/...", and the SPA catch-all would answer the bare path.
    app.add_api_route(
        ADMIN_PREFIX,
        admin_index_redirect,
        include_in_schema=False,
        methods=["GET", "HEAD"],
    )


def admin_index_redirect() -> RedirectResponse:
    """Send the bare admin path to the mounted admin's index."""
    return RedirectResponse(f"{ADMIN_PREFIX}/")


def unmatched_api_route(request: Request, unmatched: str) -> JSONResponse:
    """Return a problem response for an unknown API path."""
    return problem(
        request,
        404,
        "No such endpoint",
        f"/api/{unmatched} is not an endpoint of this API.",
        "urn:clearscout:problem:not-found",
    )


def frontend_application(
    request: Request,
    frontend_path: str,
) -> FileResponse:
    """Serve frontend files before falling back to the client-side shell."""
    frontend = request_api_state(request).frontend_directory
    if frontend is None:
        raise RuntimeError("ClearScout frontend assets were not configured.")
    frontend_root = frontend.resolve()
    asset = (frontend_root / frontend_path).resolve()
    if asset.is_file() and asset.is_relative_to(frontend_root):
        return FileResponse(asset)
    return FileResponse(frontend / "index.html")


def api_only_root() -> ApiOnlyRootResponse:
    """Describe how to reach the API when no web build is installed."""
    return ApiOnlyRootResponse(
        name="ClearScout local API",
        detail="Open the API contract to inspect this API-only installation.",
        documentation=f"{VERSIONED_API_PREFIX}/openapi.json",
    )


def _frontend_directory() -> Path | None:
    """Locate either the packaged web client or the clone's generated build."""
    package_root = Path(__file__).resolve().parent.parent
    candidates = (
        package_root / "web",
        package_root.parents[1] / "web" / "dist",
    )
    return next(
        (
            path
            for path in candidates
            if (path / "index.html").is_file() and (path / "assets").is_dir()
        ),
        None,
    )
