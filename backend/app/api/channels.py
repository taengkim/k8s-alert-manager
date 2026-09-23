"""Notification channel CRUD + test-send API.

This only manages channel *instances* (create/list/update/delete/test) --
which alerts actually reach a channel is Phase 9 (routing rules + outbox).
Channel type discovery itself lives in `app/channels/registry.py`.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any, get_args

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_channel_registry, get_current_user, require_team_role
from app.channels.base import ChannelDeliveryError, NotificationChannel
from app.channels.registry import ChannelRegistry
from app.db import get_session
from app.models.channel import Channel
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.security import decrypt_str, encrypt_str
from app.services import audit

logger = logging.getLogger(__name__)

types_router = APIRouter(prefix="/api/v1/channel-types", tags=["channels"])
router = APIRouter(prefix="/api/v1", tags=["channels"])


class ChannelCreate(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = {}


class ChannelUpdate(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


async def _get_channel_or_404(session: AsyncSession, channel_id: int) -> Channel:
    """A soft-deleted channel is 404 here -- everywhere except the
    notifications-history join (which needs the row to still resolve a
    name) treats it as gone.
    """
    result = await session.execute(
        select(Channel).where(Channel.id == channel_id, Channel.deleted_at.is_(None))
    )
    channel = result.scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    return channel


async def _require_team_role(
    session: AsyncSession, team_id: int, user: User, role: str
) -> None:
    """Same RBAC rule as `deps.require_team_role`, but callable after the
    fact once a channel's team_id is known -- `/channels/{id}` routes are
    keyed by channel id, not team_id, so the path-param-driven dependency
    factory doesn't apply here.
    """
    if user.is_admin:
        return
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    if role == "owner" and membership.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _is_secret_annotation(annotation: Any) -> bool:
    if annotation is SecretStr:
        return True
    return any(arg is SecretStr for arg in get_args(annotation))


def _mask_secrets(config: dict[str, Any], schema_cls: type[BaseModel] | None) -> dict[str, Any]:
    """Config is returned as-is (recipients etc. aren't secret) except for
    any field a channel type's schema declares as `SecretStr` -- masked so
    e.g. a webhook plugin's API key/token doesn't round-trip to the UI in
    plaintext. None of the built-in channels currently declare one; this is
    for third-party plugin schemas.
    """
    if schema_cls is None:
        return config
    masked = dict(config)
    for field_name, field in schema_cls.model_fields.items():
        if field_name in masked and _is_secret_annotation(field.annotation):
            masked[field_name] = "***"
    return masked


def _decrypt_config(channel: Channel) -> dict[str, Any]:
    return json.loads(decrypt_str(channel.config_encrypted))


def _encrypt_config(config: BaseModel) -> str:
    return encrypt_str(json.dumps(config.model_dump(mode="json")))


def _validate_config(
    channel_cls: type[NotificationChannel], raw_config: dict[str, Any]
) -> BaseModel:
    try:
        return channel_cls.config_schema(**raw_config)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.errors()
        ) from exc


def _serialize(channel: Channel, registry: ChannelRegistry) -> dict[str, Any]:
    config = _decrypt_config(channel)
    channel_cls = registry.get(channel.type)
    schema_cls = channel_cls.config_schema if channel_cls else None
    return {
        "id": channel.id,
        "team_id": channel.team_id,
        "name": channel.name,
        "type": channel.type,
        "enabled": channel.enabled,
        "config": _mask_secrets(config, schema_cls),
    }


@types_router.get("")
async def list_channel_types(
    _user: User = Depends(get_current_user),
    registry: ChannelRegistry = Depends(get_channel_registry),
) -> list[dict[str, Any]]:
    return registry.list()


@router.get("/teams/{team_id}/channels")
async def list_channels(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    registry: ChannelRegistry = Depends(get_channel_registry),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(Channel).where(Channel.team_id == team_id, Channel.deleted_at.is_(None))
    )
    return [_serialize(channel, registry) for channel in result.scalars().all()]


@router.post("/teams/{team_id}/channels", status_code=status.HTTP_201_CREATED)
async def create_channel(
    team_id: int,
    body: ChannelCreate,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
    registry: ChannelRegistry = Depends(get_channel_registry),
) -> dict[str, Any]:
    await _get_team_or_404(session, team_id)

    channel_cls = registry.get(body.type)
    if channel_cls is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown channel type"
        )
    validated_config = _validate_config(channel_cls, body.config)

    channel = Channel(
        team_id=team_id,
        name=body.name,
        type=body.type,
        config_encrypted=_encrypt_config(validated_config),
        created_by=actor.id,
    )
    session.add(channel)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="channel name already exists"
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team_id,
        action="channel.create",
        object_type="channel",
        object_ref=channel.name,
        detail={"type": channel.type},
    )
    await session.commit()
    await session.refresh(channel)
    return _serialize(channel, registry)


@router.patch("/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    registry: ChannelRegistry = Depends(get_channel_registry),
) -> dict[str, Any]:
    channel = await _get_channel_or_404(session, channel_id)
    await _require_team_role(session, channel.team_id, actor, "owner")

    if body.config is not None:
        channel_cls = registry.get(channel.type)
        if channel_cls is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="unknown channel type"
            )
        validated_config = _validate_config(channel_cls, body.config)
        channel.config_encrypted = _encrypt_config(validated_config)

    if body.name is not None:
        channel.name = body.name
    if body.enabled is not None:
        channel.enabled = body.enabled

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="channel name already exists"
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=channel.team_id,
        action="channel.update",
        object_type="channel",
        object_ref=channel.name,
    )
    await session.commit()
    await session.refresh(channel)
    return _serialize(channel, registry)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Soft-delete: sets `deleted_at` rather than removing the row.

    `notification_outbox.channel_id` is a NOT NULL FK with no ondelete, so
    a channel's row has to keep existing for as long as its delivery
    history does -- the notifications-history join
    (GET .../history/{id}/notifications) still resolves its name after
    this. A soft-deleted channel drops out of every listing/picker and
    can no longer be routed to (see route_event and the outbox worker),
    and its name becomes reusable by a new channel (the uniqueness
    constraint only applies among non-deleted rows).
    """
    channel = await _get_channel_or_404(session, channel_id)
    await _require_team_role(session, channel.team_id, actor, "owner")

    channel.deleted_at = datetime.now(UTC)

    await audit.log(
        session,
        user_id=actor.id,
        team_id=channel.team_id,
        action="channel.delete",
        object_type="channel",
        object_ref=channel.name,
    )
    await session.commit()


@router.post("/channels/{channel_id}/test", status_code=status.HTTP_202_ACCEPTED)
async def test_channel(
    channel_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    registry: ChannelRegistry = Depends(get_channel_registry),
) -> dict[str, Any]:
    channel = await _get_channel_or_404(session, channel_id)
    await _require_team_role(session, channel.team_id, actor, "member")

    channel_cls = registry.get(channel.type)
    if channel_cls is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown channel type"
        )

    config = channel_cls.config_schema(**_decrypt_config(channel))
    instance = channel_cls(config)

    try:
        await instance.send_test()
    except ChannelDeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=channel.team_id,
        action="channel.test",
        object_type="channel",
        object_ref=channel.name,
    )
    await session.commit()
    return {"status": "sent"}
