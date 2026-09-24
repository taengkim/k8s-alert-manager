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
    """Falls back to `default` when the key is unset, when a stored value
    somehow isn't a valid int (e.g. hand-edited in the database), AND when
    it parses but isn't a positive integer -- every caller of this so far
    (`app.services.retention`'s purge windows) treats "N" as "N days", so a
    stored `0` or negative value must not silently become "purge
    everything older than negative-N days" (i.e. everything). A malformed
    or out-of-range setting must degrade to "use the default", not break
    (or worse, run away with) whatever reads it.
    """
    raw = await get_setting(session, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


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
