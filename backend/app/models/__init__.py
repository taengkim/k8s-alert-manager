"""SQLAlchemy models.

Import every model module here so `Base.metadata` is fully populated for
`Base.metadata.create_all(...)` (tests) and Alembic autogenerate.
"""

from app.models.alert import AlertEvent
from app.models.audit import AuditLog
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.comment import AlertComment
from app.models.outbox import NotificationOutbox
from app.models.report import ReportSchedule, report_schedule_channels
from app.models.routing import (
    RoutingMatcher,
    RoutingRule,
    routing_rule_channels,
    routing_rule_escalation_channels,
)
from app.models.scheduled import ScheduledAction
from app.models.settings import AppSetting
from app.models.share import AlertShare
from app.models.silence import SilenceAudit
from app.models.team import Team, TeamLdapMapping, TeamMembership
from app.models.template import MessageTemplate
from app.models.user import User

__all__ = [
    "AlertComment",
    "AlertEvent",
    "AlertShare",
    "AppSetting",
    "AuditLog",
    "Channel",
    "Cluster",
    "MessageTemplate",
    "NotificationOutbox",
    "ReportSchedule",
    "RoutingMatcher",
    "RoutingRule",
    "ScheduledAction",
    "SilenceAudit",
    "Team",
    "TeamLdapMapping",
    "TeamMembership",
    "User",
    "report_schedule_channels",
    "routing_rule_channels",
    "routing_rule_escalation_channels",
]
