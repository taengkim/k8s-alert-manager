from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_k8s_factory
from app.db import get_session
from app.models.cluster import Cluster
from app.models.user import User
from app.services.k8s import K8sBadRequestError, K8sClientFactory, K8sUnavailableError

router = APIRouter(prefix="/api/v1/clusters", tags=["clusters"])
namespaces_router = APIRouter(prefix="/api/v1", tags=["clusters"])


@router.get("")
async def list_clusters(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Minimal cluster listing for Phase 2a.

    Deliberately returns only id/name/display_name/enabled: no credentials,
    no URLs. Full cluster CRUD/detail lands in Phase 11.
    """
    result = await session.execute(select(Cluster))
    return [
        {
            "id": c.id,
            "name": c.name,
            "display_name": c.display_name,
            "enabled": c.enabled,
        }
        for c in result.scalars().all()
    ]


@namespaces_router.get("/namespaces")
async def list_namespaces(
    cluster_id: int = Query(...),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
) -> list[str]:
    cluster = await session.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")

    try:
        return await k8s.list_namespaces(cluster)
    except K8sBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except K8sUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
