"""Cross-team alert sharing (Phase 14): an owner team grants a target team
read (`view`) -- or read+notify (`view_notify`) -- visibility into its own
alert stream. See `app/services/sharing.py` for how `matchers` is evaluated
and `app/services/routing.py`'s `route_event` for the `view_notify` fan-out
into the target team's own routing rules.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base, UTCDateTime


class AlertShare(Base):
    """One owner-team -> target-team sharing grant.

    `mode='view'`: the target team sees the owner's matching alerts in its
    own live/history views (read-only) -- no notification. `mode='view_notify'`:
    additionally, the target team's own `include_shared=true` routing rules
    are evaluated against the owner's alerts (see `route_event`), so the
    target can be notified through its own channels.

    `matchers` is the same include/exclude structure as `RoutingMatcher`
    rows (see `app/services/sharing.py`'s `share_matches`), stored as a
    plain JSON list of dicts rather than its own child table -- a share's
    scope is small and always rewritten as a whole on edit (no per-matcher
    CRUD, unlike a routing rule's matchers). None/[] means "every alert
    this team owns".

    CASCADE both ways on the team FKs: deleting either the owner or target
    team must not leave a dangling share row pointing at a team that no
    longer exists.
    """

    __tablename__ = "alert_shares"
    __table_args__ = (
        UniqueConstraint("owner_team_id", "target_team_id", name="uq_alert_share_owner_target"),
        CheckConstraint("owner_team_id != target_team_id", name="ck_alert_share_owner_ne_target"),
        # The owner/target UQ above is a composite index led by owner_team_id,
        # so it doesn't help a lookup by target_team_id alone -- and that's
        # exactly what every read path queries by (shared_source_team_ids:
        # "who has shared alerts with me", called on every team-scoped
        # /live, /history, and /ack-status request).
        Index("ix_alert_shares_target_team_id", "target_team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    target_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(16))  # 'view' | 'view_notify'
    matchers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # ON DELETE SET NULL: deleting the creating user must not delete (or
    # block deleting) the share -- same pattern as Channel.created_by.
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
