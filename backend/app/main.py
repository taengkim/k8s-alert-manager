import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.ops import router as ops_router
from app.config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("starting %s", settings.app_name)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="k8s-alert-manager", lifespan=lifespan)
    app.include_router(ops_router)
    return app


app = create_app()
