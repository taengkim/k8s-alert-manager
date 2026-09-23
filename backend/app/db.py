from collections.abc import AsyncGenerator
from datetime import UTC

from sqlalchemy import DateTime, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

settings = get_settings()


def register_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Turn on FK enforcement + WAL for a SQLite engine. No-op for any
    other dialect (Postgres enforces FKs natively and doesn't have this
    journal-mode knob).

    FK enforcement matters more than it looks: SQLite reuses a table's
    rowid after a row is deleted (none of our tables use AUTOINCREMENT),
    so if `ON DELETE CASCADE` never actually fires because enforcement
    defaults to off, a deleted routing rule's orphaned `routing_matchers`/
    `routing_rule_channels` rows can silently reattach themselves to a
    *different*, later-inserted rule that happens to reuse the same id --
    without FK enforcement nothing ever cleans them up first. WAL mode +
    a busy_timeout matter because the embedded outbox worker writes
    against the same on-disk file as ingest requests every ~3s; without
    them, concurrent writers can hit "database is locked" under any real
    concurrency instead of just waiting briefly for the lock.
    """
    if engine.sync_engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


engine = create_async_engine(settings.database_url)
register_sqlite_pragmas(engine)
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
