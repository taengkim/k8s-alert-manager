from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.config import get_settings
from app.models.cluster import Cluster
from app.security import hash_token
from tests.conftest import login_as


async def test_default_cluster_is_seeded_by_bootstrap(client: AsyncClient) -> None:
    settings = get_settings()
    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(
                select(Cluster).where(Cluster.name == settings.default_cluster_name)
            )
        ).scalar_one()
        assert cluster.prometheus_url == settings.prometheus_url
        assert cluster.alertmanager_url == settings.alertmanager_url
        assert cluster.webhook_token_hash == hash_token(settings.webhook_token)


async def test_get_clusters_returns_seeded_cluster_without_sensitive_fields(
    client: AsyncClient,
) -> None:
    await login_as(client, username="alice")

    response = await client.get("/api/v1/clusters")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1

    entry = body[0]
    assert set(entry.keys()) == {"id", "name", "display_name", "enabled"}
    assert entry["name"] == get_settings().default_cluster_name


async def test_get_clusters_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/clusters")
    assert response.status_code == 401
