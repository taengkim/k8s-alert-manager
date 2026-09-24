from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(default=True)

    k8s_auth_kind: Mapped[str] = mapped_column(
        String(16), default="kubeconfig"
    )  # 'incluster' | 'kubeconfig' | 'token'
    k8s_api_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    credentials_encrypted: Mapped[str | None] = mapped_column(String, nullable=True)

    prometheus_url: Mapped[str] = mapped_column(String(512))
    alertmanager_url: Mapped[str] = mapped_column(String(512))
    grafana_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    rules_namespace: Mapped[str] = mapped_column(String(255), default="kam-rules")
    webhook_token_hash: Mapped[str] = mapped_column(String(64), unique=True)

    heartbeat_enabled: Mapped[bool] = mapped_column(default=True)
    heartbeat_alertname: Mapped[str] = mapped_column(String(255), default="Watchdog")
    heartbeat_timeout_seconds: Mapped[int] = mapped_column(default=600)
    heartbeat_team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id"), nullable=True
    )
    # Phase 17: `UTCDateTime`, not plain `DateTime(timezone=True)` -- this
    # field is compared arithmetically (`now - last_heartbeat_at`) by
    # app.worker.heartbeat.sweep against a cluster row loaded fresh in its
    # own session/transaction, unlike every other heartbeat mutation before
    # Phase 17 (app.services.ingest's hook only ever read/wrote it on an
    # already-loaded ORM object within the same transaction). SQLite drops
    # the UTC offset on write regardless of `timezone=True` and hands back a
    # naive datetime on a fresh read -- `UTCDateTime` re-attaches it so that
    # arithmetic doesn't raise "can't subtract offset-naive and
    # offset-aware datetimes" the moment it's read from a different session.
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    heartbeat_state: Mapped[str] = mapped_column(String(16), default="unknown")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
