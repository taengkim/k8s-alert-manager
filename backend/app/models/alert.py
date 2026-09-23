from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base, UTCDateTime


class AlertEvent(Base):
    """One "episode" of an Alertmanager alert, as delivered by its webhook.

    Identity is (cluster_id, fingerprint, starts_at): Alertmanager keeps a
    stable fingerprint for a given label set, but assigns a *new* startsAt
    each time that label set re-fires after resolving -- so this composite
    key is what actually distinguishes separate incidents of "the same"
    alert, across possibly-colliding fingerprints from different clusters.
    """

    __tablename__ = "alert_events"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id", "fingerprint", "starts_at", name="uq_alert_event_identity"
        ),
        Index("ix_alert_events_team_status", "team_id", "status"),
        Index("ix_alert_events_cluster_status", "cluster_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"))
    # Denormalized so history rows keep a readable cluster label even if the
    # cluster is later renamed.
    cluster_name: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))  # 'firing' | 'resolved'
    alertname: Mapped[str] = mapped_column(String(255), index=True)
    # Denorm from labels, lowercased; NULL when the alert carries no
    # severity label at all.
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON)
    annotations: Mapped[dict[str, Any]] = mapped_column(JSON)
    # ON DELETE SET NULL: deleting a team must not lose this event's
    # history -- it just becomes unattributed, same as one whose
    # `kam_team` label never matched a team slug.
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    generator_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    first_received_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC)
    )
    last_received_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC)
    )
    receive_count: Mapped[int] = mapped_column(default=1)
