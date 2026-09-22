from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db import get_session
from app.models.cluster import Cluster
from app.models.user import User

router = APIRouter(prefix="/api/v1/clusters", tags=["clusters"])


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
