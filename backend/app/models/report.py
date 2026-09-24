"""Scheduled report generation + delivery (Phase 20): a team's own recurring
cadence (weekly/daily/monthly) for auto-sending an alert-activity report to
a set of channels.

Deliberately its own periodic sweep (`app.worker.scheduler.run_report_sweep`
+ `app.services.reports`), NOT a `ScheduledAction` kind (see
`app.models.scheduled`'s docstring): a `ScheduledAction` is a one-shot timer
keyed to a single alert event/routing rule, whereas a `ReportSchedule` is a
standing, recurring schedule with no event of its own -- closer in shape to
`Channel`'s always-existing config than to a one-shot timer, so it gets its
own table and its own claim/dispatch/next-run lifecycle instead of trying to
force it into the escalation/renotify/digest_flush claim vocabulary.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime

# Many-to-many: which channels a report is sent through. CASCADE both ways,
# same reasoning as app.models.routing.routing_rule_channels -- deleting a
# schedule drops its channel assignments, deleting a channel drops it from
# any schedule that referenced it, without orphaning either side.
report_schedule_channels = Table(
    "report_schedule_channels",
    Base.metadata,
    Column(
        "report_schedule_id",
        ForeignKey("report_schedules.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("channel_id", ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True),
)


class ReportSchedule(Base):
    __tablename__ = "report_schedules"
    __table_args__ = (
        # Backs app.worker.scheduler.claim_due_reports's due-row scan --
        # same shape as ix_scheduled_actions_status_due /
        # ix_outbox_status_next_attempt.
        Index("ix_report_schedules_enabled_next_run", "enabled", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ON DELETE CASCADE: a report schedule has no meaning once its team is
    # gone. Unlike Channel/MessageTemplate (soft-deleted / SET NULL
    # elsewhere), deleting a team is already a hard, admin-only, destructive
    # operation (see app/api/teams.py's delete_team) that a schedule should
    # simply go with rather than survive as an orphan.
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 'weekly' | 'daily' | 'monthly' -- app-validated, not a DB enum (same
    # convention as NotificationOutbox.status, Channel.digest_mode, etc).
    cadence: Mapped[str] = mapped_column(String(16), default="weekly")
    # 0=Monday .. 6=Sunday. Only meaningful (and only app-required) when
    # cadence='weekly' -- see app.api.reports's validation. 'daily' fires
    # every day at `hour`; 'monthly' always fires on the 1st at `hour` (see
    # app.services.reports.compute_next_run) -- neither has a day selector
    # of its own.
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hour: Mapped[int] = mapped_column(Integer)
    # IANA zone name, validated against zoneinfo at the API layer. A value
    # that somehow fails to load at sweep time (e.g. the deployment's tzdata
    # went missing after this row was saved) surfaces as this schedule's own
    # last_status='error: ...' rather than crashing the sweep for every
    # other schedule -- see app.worker.scheduler.dispatch_report_schedule.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # This schedule's own report template (kind='report' -- app-validated at
    # save time; app.services.reports.render_report defensively re-checks
    # kind at render time too, since a template's kind can be edited after
    # the fact). ON DELETE SET NULL: deleting the template just reverts this
    # schedule to the built-in default report template, same pattern as
    # Channel.template_id.
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_templates.id", ondelete="SET NULL"), nullable=True
    )
    next_run_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # 'ok' | 'error: ...' (truncated) -- app-only, surfaced on the Reports
    # tab's status Tag. None until the schedule has ever fired.
    last_status: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))

    channels: Mapped[list["Channel"]] = relationship(  # noqa: F821
        "Channel", secondary=report_schedule_channels
    )
