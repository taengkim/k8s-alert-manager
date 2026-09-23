"""AppSetting reads/writes: a tiny app-wide key/value store. `value` is
always a plain string; every caller here parses it back to whatever type it
actually is (int, an ISO timestamp, ...) -- there's no other setting kind
yet to justify a typed value column (see `app.models.settings.AppSetting`).

Currently used for the retention purge windows (`app.services.retention`)
and that service's own 'retention.last_purge_at' bookkeeping.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import AppSetting

LAST_PURGE_AT_KEY = "retention.last_purge_at"


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else None


async def get_int_setting(session: AsyncSession, key: str, default: int) -> int:
    """Falls back to `default` both when the key is unset and when a stored
    value somehow isn't a valid int (e.g. hand-edited in the database) --
    a malformed setting must degrade to "use the default", not break every
    caller that reads it.
    """
    raw = await get_setting(session, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def get_last_purge_at(session: AsyncSession) -> datetime | None:
    raw = await get_setting(session, LAST_PURGE_AT_KEY)
    return datetime.fromisoformat(raw) if raw is not None else None


async def set_last_purge_at(session: AsyncSession, when: datetime) -> None:
    await set_setting(session, LAST_PURGE_AT_KEY, when.astimezone(UTC).isoformat())
