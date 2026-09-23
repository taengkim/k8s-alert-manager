"""Alertmanager webhook receiver.

Deliberately isolated from the cookie/JWT-authenticated API surface: this
router carries no `get_current_user` dependency at all. Auth here is a
per-cluster bearer token (hashed and matched against
`clusters.webhook_token_hash`), since the caller is Alertmanager itself,
not a logged-in user.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.cluster import Cluster
from app.security import hash_token
from app.services.ingest import AlertmanagerWebhookPayload, ingest_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhook", tags=["webhook"])

# Single generic reason for every auth failure (missing header, unknown
# token, disabled cluster) -- the response body must not tell a caller
# which of those it was.
_UNAUTHORIZED_DETAIL = "unauthorized"


def _extract_bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def _authenticate_cluster(session: AsyncSession, token: str) -> Cluster | None:
    result = await session.execute(
        select(Cluster).where(
            Cluster.webhook_token_hash == hash_token(token),
            Cluster.enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


@router.post("/alertmanager")
async def receive_alertmanager_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = _extract_bearer_token(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHORIZED_DETAIL)

    cluster = await _authenticate_cluster(session, token)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHORIZED_DETAIL)

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid JSON body"
        ) from exc

    try:
        payload = AlertmanagerWebhookPayload.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="malformed webhook payload"
        ) from exc

    result = await ingest_webhook(session, cluster, payload)
    await session.commit()

    return {
        "received": result.received,
        "created": result.created,
        "created_resolved": result.created_resolved,
        "resolved": result.resolved,
        "reopened": result.reopened,
        "repeats": result.repeats,
        "heartbeats_seen": result.heartbeats_seen,
        "skipped": result.skipped,
    }
