import asyncio
import logging
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import app.db as db_module
from app.api.admin_settings import router as admin_settings_router
from app.api.alerts import comments_router as alerts_comments_router
from app.api.alerts import router as alerts_router
from app.api.alerts import team_router as alerts_team_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.channels import router as channels_router
from app.api.channels import types_router as channel_types_router
from app.api.clusters import namespaces_router
from app.api.clusters import router as clusters_router
from app.api.events import router as events_router
from app.api.metrics import router as metrics_router
from app.api.ops import router as ops_router
from app.api.reports import router as reports_router
from app.api.routes import router as routes_router
from app.api.routes import team_router as routes_team_router
from app.api.rules import router as rules_router
from app.api.rules import validate_router as rules_validate_router
from app.api.shares import router as shares_router
from app.api.shares import shared_with_me_router
from app.api.shares import team_router as shares_team_router
from app.api.silences import router as silences_router
from app.api.stats import router as stats_router
from app.api.teams import router as teams_router
from app.api.templates import router as templates_router
from app.api.users import router as users_router
from app.api.webhook import router as webhook_router
from app.channels.registry import ChannelRegistry
from app.config import get_settings
from app.services.cluster_bootstrap import ensure_default_cluster
from app.services.cluster_health import ClusterHealthCache
from app.services.events_hub import Hub
from app.services.k8s import K8sClientFactory
from app.worker.outbox import run_loop

logger = logging.getLogger(__name__)

# Built SPA output (`frontend/` -> `npm run build` -> `dist/`), copied here by
# the packaging Dockerfile's final stage. Absent in dev (host-run backend
# against the Vite dev server) and in the test app fixture -- the static
# mount + SPA fallback route below are only registered when this directory
# actually exists, so neither dev flow nor tests are affected.
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("starting %s", settings.app_name)

    # Shared client for outbound calls to per-cluster Alertmanager/Prometheus
    # instances, reused across requests instead of reconnecting each time.
    app.state.http_client = httpx.AsyncClient()

    # Caches per-cluster kubernetes ApiClients (see K8sClientFactory docstring
    # for the cache-invalidation rule).
    app.state.k8s_factory = K8sClientFactory()

    # 30s in-process cache of per-cluster health probes (see
    # ClusterHealthCache docstring) -- one instance per app, same rationale
    # as k8s_factory above.
    app.state.cluster_health_cache = ClusterHealthCache()

    # Phase 18 live SSE feed's broadcast hub. Exists unconditionally --
    # unlike the embedded outbox worker below, there's no "off" mode for
    # this (see app/api/events.py's docstring): the test app fixture uses
    # it the same way a real deployment does, just with nothing but tests
    # publishing to it.
    app.state.events_hub = Hub()

    # Discovered once at startup: built-in channels + entry-point/plugins-dir
    # third-party channels (see app/channels/registry.py). discover() already
    # isolates a single broken plugin file/class -- this try/except is a
    # second line of defense so an unanticipated failure there still can't
    # take the whole app down, the same posture as ensure_default_cluster
    # below.
    app.state.channel_registry = ChannelRegistry()
    try:
        app.state.channel_registry.discover()
    except Exception:
        logger.exception("failed to discover notification channels")

    try:
        async with db_module.async_session_factory() as session:
            await ensure_default_cluster(session)
    except Exception:
        # Don't block startup on a seeding failure (e.g. migrations not run
        # yet); the app will just have no default cluster until retried.
        logger.exception("failed to seed default cluster")

    # Embedded outbox worker: runs as a background task inside this same
    # process. 'off' is for a standalone `python -m app.worker.runner`
    # process instead (or the test app fixture, which must never race
    # tests with its own delivery attempts).
    worker_task: asyncio.Task | None = None
    stop_event = asyncio.Event()
    if settings.worker_mode == "embedded":
        worker_id = f"embedded-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        worker_task = asyncio.create_task(
            run_loop(
                stop_event,
                # Read here (lifespan startup), not at import time -- the
                # test app fixture repoints this module attribute at its
                # own in-memory engine before entering the lifespan context,
                # so this already sees that override when worker_mode isn't
                # 'off'.
                session_factory=db_module.async_session_factory,
                registry=app.state.channel_registry,
                worker_id=worker_id,
                # Phase 18: lets the heartbeat sweep publish alert_created
                # for a synthetic heartbeat-lost alert it injects -- see
                # run_loop's own docstring for why only this embedded path
                # (not the standalone runner) passes one.
                hub=app.state.events_hub,
            )
        )

    yield

    if worker_task is not None:
        stop_event.set()
        await worker_task

    await app.state.http_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="k8s-alert-manager", lifespan=lifespan)
    app.include_router(ops_router)
    app.include_router(auth_router)
    app.include_router(teams_router)
    app.include_router(users_router)
    app.include_router(admin_settings_router)
    app.include_router(clusters_router)
    app.include_router(namespaces_router)
    app.include_router(alerts_router)
    app.include_router(alerts_comments_router)
    app.include_router(alerts_team_router)
    app.include_router(rules_router)
    app.include_router(rules_validate_router)
    app.include_router(metrics_router)
    app.include_router(silences_router)
    app.include_router(webhook_router)
    app.include_router(events_router)
    app.include_router(channel_types_router)
    app.include_router(channels_router)
    app.include_router(routes_team_router)
    app.include_router(routes_router)
    app.include_router(templates_router)
    app.include_router(shares_team_router)
    app.include_router(shares_router)
    app.include_router(shared_with_me_router)
    app.include_router(stats_router)
    app.include_router(audit_router)
    app.include_router(reports_router)

    # SPA static serving (packaged/deployed image only -- see STATIC_DIR's
    # docstring). Registered LAST so every `/api/v1/*` router above always
    # wins the route match first; the catch-all below only ever sees a
    # request none of them claimed.
    if STATIC_DIR.is_dir():
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="spa-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> FileResponse:
            # An unmatched `/api/...` request is a real 404 (unknown API
            # route), never the SPA shell -- falling through to index.html
            # here would turn a client's typo'd/removed endpoint into a
            # confusing 200-with-HTML instead of a clear 404.
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")
            candidate = STATIC_DIR / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            # Any other path (the app root, or a client-side route like
            # /alerts/123 with no matching file) falls back to the SPA
            # shell -- React Router resolves the actual view client-side.
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
