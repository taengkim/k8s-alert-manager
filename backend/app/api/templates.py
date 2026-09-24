"""Message template CRUD + preview + variable-reference API (Phase 13).

Rendering itself lives in `app/services/templating.py`; this module is the
HTTP surface: team-scoped CRUD (owner-gated for writes, member-readable,
matching this app's usual per-team RBAC split -- see `app/api/channels.py`
and `app/api/routes.py` for the same pattern), a preview endpoint the
editor's live-preview panel polls, and the variable-reference list its
sidebar renders.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_team_role
from app.channels.base import AlertNotification
from app.db import get_session
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.routing import RoutingRule
from app.models.team import Team, TeamMembership
from app.models.template import MAX_TEMPLATE_LENGTH, MessageTemplate
from app.models.user import User
from app.services import audit
from app.services.reports import REPORT_TEMPLATE_KIND, report_template_variables
from app.services.routing import build_notification_for_event
from app.services.templating import preview as render_preview
from app.services.templating import template_variables, validate_template_strings

router = APIRouter(prefix="/api/v1", tags=["templates"])

# 'alert' (Phase 13) + 'report' (Phase 20, REPORT_TEMPLATE_KIND) -- see
# app/models/template.py's docstring for why the column itself has no
# DB-level CHECK constraint.
SUPPORTED_KINDS = {"alert", REPORT_TEMPLATE_KIND}


class TemplateWrite(BaseModel):
    name: str
    description: str | None = None
    kind: str = "alert"
    title_template: str
    body_template: str
    body_html_template: str | None = None


class PreviewRequest(BaseModel):
    title_template: str
    body_template: str
    body_html_template: str | None = None
    # Neither given -> render against a synthetic sample alert
    # (AlertNotification.example()). alert_event_id, when given, renders
    # against that real event's data instead (subject to the same
    # team-membership access check as GET .../alerts/history/{id}).
    alert_event_id: int | None = None
    use_sample: bool = False


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


async def _get_template_or_404(session: AsyncSession, template_id: int) -> MessageTemplate:
    template = await session.get(MessageTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="template not found")
    return template


async def _require_team_role(session: AsyncSession, team_id: int, user: User, role: str) -> None:
    """Same RBAC rule as `deps.require_team_role`, callable after the fact
    once a template's team_id is known -- `/templates/{id}` is keyed by
    template id, not team_id, so the path-param-driven dependency factory
    doesn't apply.
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


async def _authorize_event_access(session: AsyncSession, event: AlertEvent, user: User) -> None:
    """Same access rule as `app/api/alerts.py`'s own event-detail
    authorization: an admin, or a member of the event's team. Duplicated
    (not imported) since that's a module-private helper there -- same
    reasoning as `_require_team_role`'s duplication across every API module
    that needs it (`channels.py`, `routes.py`, here).
    """
    if user.is_admin:
        return
    if event.team_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == event.team_id, TeamMembership.user_id == user.id
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


async def _require_any_team_membership(session: AsyncSession, user: User) -> None:
    """Gate for `POST /templates/preview`: an admin, or a member of *some*
    team -- not scoped to any specific team_id (preview has no team_id path
    param; a real event's own team is separately checked by
    `_authorize_event_access` when `alert_event_id` is given). A user
    belonging to zero teams has no legitimate reason to drive template
    compilation (each preview call queues onto `templating.RENDER_EXECUTOR`,
    a shared, capacity-limited resource -- see that module's docstring), so
    this is a coarse pre-filter against exactly that, not a substitute for
    the per-event check.
    """
    if user.is_admin:
        return
    result = await session.execute(
        select(TeamMembership.id).where(TeamMembership.user_id == user.id).limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _validate_body(body: TemplateWrite) -> None:
    if body.kind not in SUPPORTED_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unsupported kind '{body.kind}' (supported: {sorted(SUPPORTED_KINDS)})",
        )
    for field_name, value in (
        ("title_template", body.title_template),
        ("body_template", body.body_template),
        ("body_html_template", body.body_html_template),
    ):
        if value is not None and len(value) > MAX_TEMPLATE_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{field_name} exceeds {MAX_TEMPLATE_LENGTH} characters",
            )

    errors = validate_template_strings(
        {
            "title": body.title_template,
            "body": body.body_template,
            "body_html": body.body_html_template,
        }
    )
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)


async def _usage_counts(
    session: AsyncSession, template_ids: list[int]
) -> dict[int, dict[str, int]]:
    """How many (non-deleted) channels and routing rules currently point at
    each of `template_ids` -- the list page's "사용처 수" column, and what
    a delete's response detail summarizes.
    """
    if not template_ids:
        return {}

    channel_rows = (
        await session.execute(
            select(Channel.template_id, func.count())
            .where(Channel.template_id.in_(template_ids), Channel.deleted_at.is_(None))
            .group_by(Channel.template_id)
        )
    ).all()
    route_rows = (
        await session.execute(
            select(RoutingRule.template_id, func.count())
            .where(RoutingRule.template_id.in_(template_ids))
            .group_by(RoutingRule.template_id)
        )
    ).all()
    channel_counts = dict(channel_rows)
    route_counts = dict(route_rows)
    return {
        tid: {
            "channel_count": channel_counts.get(tid, 0),
            "route_count": route_counts.get(tid, 0),
        }
        for tid in template_ids
    }


def _serialize(template: MessageTemplate) -> dict[str, Any]:
    return {
        "id": template.id,
        "team_id": template.team_id,
        "name": template.name,
        "description": template.description,
        "kind": template.kind,
        "title_template": template.title_template,
        "body_template": template.body_template,
        "body_html_template": template.body_html_template,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


@router.get("/teams/{team_id}/templates")
async def list_templates(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(MessageTemplate)
        .where(MessageTemplate.team_id == team_id)
        .order_by(MessageTemplate.name)
    )
    templates = result.scalars().all()
    counts = await _usage_counts(session, [t.id for t in templates])
    return [{**_serialize(t), **counts[t.id]} for t in templates]


@router.post("/teams/{team_id}/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    team_id: int,
    body: TemplateWrite,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_team_or_404(session, team_id)
    _validate_body(body)

    template = MessageTemplate(
        team_id=team_id,
        name=body.name,
        description=body.description,
        kind=body.kind,
        title_template=body.title_template,
        body_template=body.body_template,
        body_html_template=body.body_html_template,
        created_by=actor.id,
    )
    session.add(template)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="template name already exists"
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team_id,
        action="template.create",
        object_type="message_template",
        object_ref=template.name,
    )
    await session.commit()
    await session.refresh(template)
    return {**_serialize(template), "channel_count": 0, "route_count": 0}


@router.post("/templates/preview")
async def preview_template(
    body: PreviewRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_any_team_membership(session, user)

    if body.alert_event_id is not None:
        event = await session.get(AlertEvent, body.alert_event_id)
        if event is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert event not found")
        await _authorize_event_access(session, event, user)
        notification = await build_notification_for_event(session, event)
    else:
        notification = AlertNotification.example()

    template_strs = {
        "title": body.title_template,
        "body": body.body_template,
        "body_html": body.body_html_template,
    }
    return await render_preview(template_strs, notification)


@router.get("/templates/variables")
async def get_template_variables(
    kind: str = Query(default="alert"),
    _user: User = Depends(get_current_user),
) -> list[dict[str, str]]:
    """The editor's variable-reference sidebar. `kind='report'` (Phase 20)
    returns `ReportData`'s own fields (see `app.services.reports`) instead
    of `AlertNotification`'s -- an 'alert'-kind and a 'report'-kind template
    render against entirely different contexts (see
    `app.services.reports.render_report` vs.
    `app.services.templating.render`), so the reference list has to match
    whichever kind is actually being edited. An unrecognized `kind` falls
    back to the alert variable list rather than 422ing -- this is a UI
    reference aid, not a validation gate (kind validation itself happens in
    `_validate_body` at save time).
    """
    if kind == REPORT_TEMPLATE_KIND:
        return report_template_variables()
    return template_variables()


# NOTE: the two static routes above (`/templates/preview`, `/templates/variables`)
# must stay registered before the dynamic `/templates/{template_id}` routes
# below -- FastAPI/Starlette matches routes by registration order, and an
# `int`-typed path param doesn't "fall through" to a later route when a
# literal segment like "preview" fails to convert; it 422s instead.


@router.get("/templates/{template_id}")
async def get_template(
    template_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    template = await _get_template_or_404(session, template_id)
    await _require_team_role(session, template.team_id, actor, "member")
    counts = await _usage_counts(session, [template.id])
    return {**_serialize(template), **counts[template.id]}


@router.put("/templates/{template_id}")
async def update_template(
    template_id: int,
    body: TemplateWrite,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    template = await _get_template_or_404(session, template_id)
    await _require_team_role(session, template.team_id, actor, "owner")
    _validate_body(body)

    template.name = body.name
    template.description = body.description
    template.kind = body.kind
    template.title_template = body.title_template
    template.body_template = body.body_template
    template.body_html_template = body.body_html_template

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="template name already exists"
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=template.team_id,
        action="template.update",
        object_type="message_template",
        object_ref=template.name,
    )
    await session.commit()
    await session.refresh(template)
    counts = await _usage_counts(session, [template.id])
    return {**_serialize(template), **counts[template.id]}


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Hard-deletes the template. Any channel/routing rule that had it
    assigned reverts to its own next-priority default (its channel type's
    `default_templates`, or the app default) via each FK's `ON DELETE SET
    NULL` -- the counts below are read *before* the delete so the response
    can say exactly how many were affected, since the SET NULL itself
    happens silently at the database level with no ORM-visible trace.
    """
    template = await _get_template_or_404(session, template_id)
    await _require_team_role(session, template.team_id, actor, "owner")

    counts = (await _usage_counts(session, [template.id]))[template.id]

    await audit.log(
        session,
        user_id=actor.id,
        team_id=template.team_id,
        action="template.delete",
        object_type="message_template",
        object_ref=template.name,
        detail=counts,
    )
    await session.delete(template)
    await session.commit()

    return {
        "deleted": True,
        "unassigned_channels": counts["channel_count"],
        "unassigned_routes": counts["route_count"],
        "detail": (
            f"이 템플릿을 사용하던 채널 {counts['channel_count']}개, "
            f"규칙 {counts['route_count']}개는 기본 템플릿으로 되돌아갑니다."
        ),
    }
