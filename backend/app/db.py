from collections.abc import AsyncGenerator
from datetime import UTC

from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """A `DateTime(timezone=True)` that always round-trips as UTC-aware.

    SQLite has no native tz-aware timestamp type: regardless of the
    column's `timezone=True`, its DATETIME storage format drops the UTC
    offset on write and the driver hands back a naive `datetime` on read.
    Every value this app stores in a `UTCDateTime` column is normalized to
    UTC before it reaches the ORM (see `_parse_am_timestamp` and the
    `datetime.now(UTC)` column defaults), so a naive value coming back is
    always UTC in fact -- this re-attaches that tzinfo instead of leaving
    callers to a bare naive datetime that's easy to mishandle as local
    time (e.g. by a frontend `Date`/dayjs parse). On drivers that do
    preserve the offset (e.g. Postgres' timestamptz), the value already
    comes back aware and this is a no-op.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        # Enforce the UTC invariant on write, not just by convention: an
        # aware non-UTC value (e.g. a +09:00 from_ts query param) would
        # otherwise have its offset silently dropped by SQLite, shifting
        # the stored/compared instant.
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


# Import model modules so they register on Base.metadata (needed for both
# create_all in tests and Alembic autogenerate). Placed at the bottom to
# avoid a circular import, since model modules do `from app.db import Base`.
from app import models  # noqa: F401
