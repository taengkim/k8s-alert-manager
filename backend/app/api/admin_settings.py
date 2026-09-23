"""Admin-only app settings API: the retention purge windows
(`app.services.retention`) and an on-demand purge trigger. Every other app
setting lives in code/env config (`app.config.Settings`) -- these are the
only ones an admin can tune at runtime without a redeploy, hence an
explicit whitelist (`app.services.retention.SETTING_DEFAULTS`) rather than a
generic key/value passthrough.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as db_module
from app.api.deps import require_admin
from app.db import get_session
from app.models.user import User
from app.services import audit
from app.services.retention import SETTING_DEFAULTS, load_settings, purge
from app.services.settings import set_setting

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class SettingsUpdate(BaseModel):
    # Every key must be one of SETTING_DEFAULTS's; every value a positive
    # integer (days) -- see update_settings's validation below.
    values: dict[str, int]


@router.get("/settings")
async def get_settings_api(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    return await load_settings(session)


@router.put("/settings")
async def update_settings_api(
    body: SettingsUpdate,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    unknown = sorted(set(body.values) - set(SETTING_DEFAULTS))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown setting keys: {unknown}",
        )
    invalid = {k: v for k, v in body.values.items() if v < 1}
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"values must be >= 1: {invalid}",
        )

    for key, value in body.values.items():
        await set_setting(session, key, str(value))

    await audit.log(
        session,
        user_id=actor.id,
        team_id=None,
        action="settings.update",
        object_type="app_setting",
        object_ref="retention",
        detail=body.values,
    )
    await session.commit()
    return await load_settings(session)


@router.post("/retention/purge")
async def run_retention_purge(actor: User = Depends(require_admin)) -> dict[str, Any]:
    """Runs the exact same purge as the daily scheduler sweep
    (`app.worker.scheduler.maybe_run_retention_sweep`), on demand -- also
    updates 'retention.last_purge_at', so this pushes the next automatic
    sweep back by a full interval rather than running twice back to back.
    """
    summary = await purge(db_module.async_session_factory, actor_user_id=actor.id)
    return {"summary": summary}
