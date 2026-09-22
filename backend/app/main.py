import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import app.db as db_module
from app.api.auth import router as auth_router
from app.api.clusters import router as clusters_router
from app.api.ops import router as ops_router
from app.api.teams import router as teams_router
from app.api.users import router as users_router
from app.config import get_settings
from app.services.cluster_bootstrap import ensure_default_cluster

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("starting %s", settings.app_name)

    try:
        async with db_module.async_session_factory() as session:
            await ensure_default_cluster(session)
    except Exception:
        # Don't block startup on a seeding failure (e.g. migrations not run
        # yet); the app will just have no default cluster until retried.
        logger.exception("failed to seed default cluster")

    yield


def create_app() -> FastAPI:
    app = FastAPI(title="k8s-alert-manager", lifespan=lifespan)
    app.include_router(ops_router)
    app.include_router(auth_router)
    app.include_router(teams_router)
    app.include_router(users_router)
    app.include_router(clusters_router)
    return app


app = create_app()
