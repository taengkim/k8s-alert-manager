"""Seed the default cluster row on startup, idempotently."""

from sqlalchemy import select

from app.config import get_settings
from app.models.cluster import Cluster
from app.security import hash_token


async def ensure_default_cluster(session) -> None:
    settings = get_settings()
    result = await session.execute(
        select(Cluster).where(Cluster.name == settings.default_cluster_name)
    )
    if result.scalar_one_or_none() is not None:
        return

    session.add(
        Cluster(
            name=settings.default_cluster_name,
            display_name=settings.default_cluster_name,
            k8s_auth_kind="kubeconfig",
            prometheus_url=settings.prometheus_url,
            alertmanager_url=settings.alertmanager_url,
            webhook_token_hash=hash_token(settings.webhook_token),
        )
    )
    await session.commit()
