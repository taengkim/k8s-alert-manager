"""App-wide key/value settings -- currently just the retention purge
knobs (`app.services.retention`) and its own last-run bookkeeping. A plain
string value column: retention settings are all integers (days), parsed by
the reader, not enforced by the schema -- there's no other setting kind yet
to justify a typed value column.
"""

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
