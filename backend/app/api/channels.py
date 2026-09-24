"""Notification channel CRUD + test-send API.

This only manages channel *instances* (create/list/update/delete/test) --
which alerts actually reach a channel is Phase 9 (routing rules + outbox).
Channel type discovery itself lives in `app/channels/registry.py`.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_channel_registry, get_current_user, require_team_role
from app.channels.base import ChannelDeliveryError, NotificationChannel
from app.channels.registry import ChannelRegistry
from app.db import get_session
from app.models.channel import Channel
from app.models.routing import (
    RoutingRule,
    routing_rule_channels,
    routing_rule_escalation_channels,
)
from app.models.team import Team, TeamMembership
from app.models.template import ALERT_TEMPLATE_KIND, MessageTemplate
from app.models.user import User
from app.security import decrypt_str, encrypt_str
from app.services import audit

logger = logging.getLogger(__name__)

types_router = APIRouter(prefix="/api/v1/channel-types", tags=["channels"])
router = APIRouter(prefix="/api/v1", tags=["channels"])


DigestMode = Literal["off", "auto", "always"]


class ChannelCreate(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = {}
    template_id: int | None = None
    # Phase 15: opts this channel in to being selectable as an ESCALATION
    # target by another team's routing rule (see
    # GET /channels/escalation-targets and app/api/routes.py's
    # _resolve_escalation_channels). Has no effect on this channel's own
    # team's rules, which can always select it either way.
    allow_cross_team_escalation: bool = False
    # Phase 16 storm control -- see app.services.routing._channel_needs_parking.
    rate_limit_per_hour: int | None = Field(default=None, gt=0)
    digest_mode: DigestMode = "off"
    digest_window_minutes: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def _validate_digest_mode(self) -> "ChannelCreate":
        if self.digest_mode == "auto" and self.rate_limit_per_hour is None:
            raise ValueError("digest_mode='auto' requires rate_limit_per_hour")
        return self


class ChannelUpdate(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    # None is ambiguous between "not provided" (leave unchanged) and
    # "explicitly clear it" -- update_channel disambiguates via
    # `body.model_fields_set` rather than the value itself, same as every
    # other Optional field on this model.
    template_id: int | None = None
    allow_cross_team_escalation: bool | None = None
    # Phase 16 storm control. `rate_limit_per_hour: None` explicitly clears
    # it (same "check model_fields_set, not the value" disambiguation as
    # template_id above) -- update_channel validates the FINAL merged state
    # (not just this body in isolation), since 'auto' requiring a rate limit
    # is a property of the channel as a whole, not of any one PATCH.
    rate_limit_per_hour: int | None = Field(default=None, gt=0)
    digest_mode: DigestMode | None = None
    digest_window_minutes: int | None = Field(default=None, gt=0)


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


async def _validate_template_ownership(
    session: AsyncSession, team_id: int, template_id: int
) -> None:
    template = await session.get(MessageTemplate, template_id)
    if template is None or template.team_id != team_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="template_id must belong to this team",
        )
    # Phase 20: a 'report'-kind template's render context (ReportData) has
    # nothing in common with a channel's own alert-delivery context
    # (AlertNotification) -- assigning one here would silently render
    # blank/nonsense output via the sandbox's lenient ChainableUndefined
    # rather than failing loudly. See app/models/template.py's docstring.
    if template.kind != ALERT_TEMPLATE_KIND:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"template_id must be an '{ALERT_TEMPLATE_KIND}'-kind template",
        )


async def _clear_escalation_for_emptied_rules(
    session: AsyncSession, candidate_rule_ids: list[int]
) -> list[int]:
    """After removing some `routing_rule_escalation_channels` rows, clear
    `escalation_enabled`/`escalation_after_minutes` on any rule among
    `candidate_rule_ids` that's left with ZERO escalation channels.

    Without this, a rule left with `escalation_enabled=True` and no
    escalation channels would 422 on every future save/toggle
    (`app/api/routes.py`'s `_validate_escalation` requires at least one
    channel whenever escalation is enabled) -- a lockout with no UI path
    out of it, since the route editor can't even load a channel picker
    option for a channel that's gone. A rule that still has at least one
    other escalation channel left is untouched. Returns the ids actually
    cleared, for the caller's audit log detail.
    """
    if not candidate_rule_ids:
        return []
    result = await session.execute(
        select(RoutingRule).where(
            RoutingRule.id.in_(candidate_rule_ids),
            RoutingRule.escalation_enabled.is_(True),
            ~RoutingRule.id.in_(
                select(routing_rule_escalation_channels.c.routing_rule_id).where(
                    routing_rule_escalation_channels.c.routing_rule_id.in_(candidate_rule_ids)
                )
            ),
        )
    )
    cleared_ids = []
    for rule in result.scalars().all():
        rule.escalation_enabled = False
        rule.escalation_after_minutes = None
        cleared_ids.append(rule.id)
    return cleared_ids


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
        "template_id": channel.template_id,
        "allow_cross_team_escalation": channel.allow_cross_team_escalation,
        "rate_limit_per_hour": channel.rate_limit_per_hour,
        "digest_mode": channel.digest_mode,
        "digest_window_minutes": channel.digest_window_minutes,
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
    if body.template_id is not None:
        await _validate_template_ownership(session, team_id, body.template_id)

    channel = Channel(
        team_id=team_id,
        name=body.name,
        type=body.type,
        config_encrypted=_encrypt_config(validated_config),
        created_by=actor.id,
        template_id=body.template_id,
        allow_cross_team_escalation=body.allow_cross_team_escalation,
        rate_limit_per_hour=body.rate_limit_per_hour,
        digest_mode=body.digest_mode,
        digest_window_minutes=body.digest_window_minutes,
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

    cleared_rule_ids: list[int] = []
    if body.allow_cross_team_escalation is False and channel.allow_cross_team_escalation:
        # Revoking cross-team consent: this channel is no longer a legal
        # escalation_channel selection for any OTHER team's rule (see
        # Channel.allow_cross_team_escalation) -- strip those join rows now,
        # rather than leaving them to be caught only defensively at dispatch
        # time (app/worker/scheduler.py's _dispatch_escalation also
        # re-checks this, but a rule editor that still lists a now-illegal
        # channel as selected is confusing on its own).
        affected_result = await session.execute(
            select(routing_rule_escalation_channels.c.routing_rule_id)
            .select_from(routing_rule_escalation_channels)
            .join(RoutingRule, RoutingRule.id == routing_rule_escalation_channels.c.routing_rule_id)
            .where(
                routing_rule_escalation_channels.c.channel_id == channel.id,
                RoutingRule.team_id != channel.team_id,
            )
        )
        affected_rule_ids = [row[0] for row in affected_result.all()]
        if affected_rule_ids:
            await session.execute(
                delete(routing_rule_escalation_channels).where(
                    routing_rule_escalation_channels.c.channel_id == channel.id,
                    routing_rule_escalation_channels.c.routing_rule_id.in_(affected_rule_ids),
                )
            )
            cleared_rule_ids = await _clear_escalation_for_emptied_rules(
                session, affected_rule_ids
            )
    if body.allow_cross_team_escalation is not None:
        channel.allow_cross_team_escalation = body.allow_cross_team_escalation
    if "template_id" in body.model_fields_set:
        if body.template_id is not None:
            await _validate_template_ownership(session, channel.team_id, body.template_id)
        channel.template_id = body.template_id

    if "rate_limit_per_hour" in body.model_fields_set:
        channel.rate_limit_per_hour = body.rate_limit_per_hour
    if "digest_mode" in body.model_fields_set and body.digest_mode is not None:
        channel.digest_mode = body.digest_mode
    if "digest_window_minutes" in body.model_fields_set and body.digest_window_minutes is not None:
        channel.digest_window_minutes = body.digest_window_minutes
    if channel.digest_mode == "auto" and channel.rate_limit_per_hour is None:
        # Validated against the channel's FINAL merged state, not just this
        # one PATCH body in isolation -- e.g. a request that only sets
        # digest_mode='auto' while rate_limit_per_hour was already NULL from
        # before (or is being cleared in the very same request) must still
        # 422, and a request that only sets rate_limit_per_hour while
        # digest_mode was already 'auto' from before must NOT 422 just
        # because this body didn't also touch digest_mode.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="digest_mode='auto' requires rate_limit_per_hour",
        )

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
        detail={"escalation_disabled_rule_ids": cleared_rule_ids} if cleared_rule_ids else None,
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

    Its `routing_rule_channels` AND `routing_rule_escalation_channels`
    (Phase 15) join rows are both hard-deleted, though -- unlike
    notification_outbox, neither table holds any history (a rule's channel
    *assignment* isn't a fact worth preserving once the channel is gone),
    and leaving them dangling would make GET .../routes/{id} keep reporting
    a channel_id every other endpoint now treats as nonexistent -- which
    then makes PUT-ing that same rule back (e.g. just toggling `enabled`)
    422 on "unknown channel_ids"/"unknown escalation_channel_ids". Any rule
    left with zero escalation channels as a result also has
    escalation_enabled cleared (see `_clear_escalation_for_emptied_rules`)
    -- otherwise that same PUT would 422 for a different reason.
    """
    channel = await _get_channel_or_404(session, channel_id)
    await _require_team_role(session, channel.team_id, actor, "owner")

    affected_escalation_rule_ids = [
        row[0]
        for row in (
            await session.execute(
                select(routing_rule_escalation_channels.c.routing_rule_id).where(
                    routing_rule_escalation_channels.c.channel_id == channel_id
                )
            )
        ).all()
    ]

    channel.deleted_at = datetime.now(UTC)
    await session.execute(
        delete(routing_rule_channels).where(routing_rule_channels.c.channel_id == channel_id)
    )
    await session.execute(
        delete(routing_rule_escalation_channels).where(
            routing_rule_escalation_channels.c.channel_id == channel_id
        )
    )
    cleared_rule_ids = await _clear_escalation_for_emptied_rules(
        session, affected_escalation_rule_ids
    )
    if cleared_rule_ids:
        logger.info(
            "channel %s delete: cleared escalation_enabled on rule(s) %s (no channels left)",
            channel_id,
            cleared_rule_ids,
        )

    await audit.log(
        session,
        user_id=actor.id,
        team_id=channel.team_id,
        action="channel.delete",
        object_type="channel",
        object_ref=channel.name,
        detail={"escalation_disabled_rule_ids": cleared_rule_ids} if cleared_rule_ids else None,
    )
    await session.commit()


@router.get("/channels/escalation-targets")
async def list_escalation_targets(
    team_id: int = Query(...),
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    """Every channel selectable as an escalation target for `team_id`'s own
    routing rules (Phase 15): all of `team_id`'s own channels, plus any
    OTHER team's channel that has opted in via
    `allow_cross_team_escalation=True` -- this is what
    `app.api.routes._resolve_escalation_channels` actually enforces at
    save time, so a channel this endpoint omits should never be pickable in
    the route editor's escalation channel Select in the first place.
    """
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(Channel, Team.slug)
        .join(Team, Team.id == Channel.team_id)
        .where(
            Channel.deleted_at.is_(None),
            or_(Channel.team_id == team_id, Channel.allow_cross_team_escalation.is_(True)),
        )
        .order_by(Team.slug, Channel.name)
    )
    return [
        {"id": channel.id, "name": channel.name, "team_slug": team_slug}
        for channel, team_slug in result.all()
    ]


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
