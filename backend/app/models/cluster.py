from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


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
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_state: Mapped[str] = mapped_column(String(16), default="unknown")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
