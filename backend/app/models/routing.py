"""Routing rules: which alert events trigger which channel notifications.

A rule's gate/filter fields (severities, namespaces, clusters) narrow which
events it applies to; its `action` decides whether a match notifies through
its channels or suppresses the event outright. `RoutingMatcher` rows add
finer include/exclude matching on alertname/label/annotation values. See
`app/services/routing.py`'s `evaluate()` for exactly how these combine into
a verdict.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base, UTCDateTime

# Many-to-many: which channels a rule notifies through. CASCADE both ways --
# deleting a rule drops its channel assignments, deleting a channel drops it
# from any rule that referenced it, without orphaning either side.
routing_rule_channels = Table(
    "routing_rule_channels",
    Base.metadata,
    Column(
        "routing_rule_id",
        ForeignKey("routing_rules.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("channel_id", ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True),
)


class RoutingRule(Base):
    __tablename__ = "routing_rules"
    __table_args__ = (UniqueConstraint("team_id", "name", name="uq_routing_rule_team_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    action: Mapped[str] = mapped_column(String(16), default="notify")  # 'notify' | 'suppress'
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_firing: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    # Phase 14 (cross-team shared-alert visibility): column reserved now,
    # gated off -- evaluate() never lets this be anything but a no-op.
    include_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    # None/[] mean "no filter" at this field -- see evaluate()'s docstring.
    severities: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    namespaces_include: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    namespaces_exclude: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    clusters: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    # This rule's own message template (Phase 13) -- takes priority over the
    # channel's template_id (see app.services.templating.resolve_template).
    # ON DELETE SET NULL: deleting the template just reverts the rule to
    # whatever the channel (or its type's default) would otherwise render.
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_templates.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    matchers: Mapped[list["RoutingMatcher"]] = relationship(
        "RoutingMatcher",
        cascade="all, delete-orphan",
        # Deleting a rule must not force an async lazy-load of its matchers
        # just to cascade-delete them one by one -- the FK's own
        # ondelete='CASCADE' already handles that at the database level.
        # (Collection-replacement on an *already-loaded* list, e.g. a PUT's
        # full matcher replace, still gets proper per-row DELETEs from the
        # delete-orphan cascade -- passive_deletes only changes what happens
        # when the parent row itself is deleted.)
        passive_deletes=True,
        order_by="RoutingMatcher.position",
    )
    channels: Mapped[list["Channel"]] = relationship(  # noqa: F821
        "Channel", secondary=routing_rule_channels
    )


class RoutingMatcher(Base):
    """One include/exclude condition on a routing rule.

    `key` is required when `target` is 'label'/'annotation' (the label or
    annotation name to read) and unused for 'alertname'; enforced at the API
    layer, not the DB. See `evaluate()`'s steps 5-6 for how these combine
    (include: AND across all include matchers; exclude: OR across all
    exclude matchers).
    """

    __tablename__ = "routing_matchers"

    id: Mapped[int] = mapped_column(primary_key=True)
    routing_rule_id: Mapped[int] = mapped_column(
        ForeignKey("routing_rules.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))  # 'include' | 'exclude'
    target: Mapped[str] = mapped_column(String(16))  # 'alertname' | 'label' | 'annotation'
    key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pattern: Mapped[str] = mapped_column(String(512))
    position: Mapped[int] = mapped_column(Integer, default=0)
