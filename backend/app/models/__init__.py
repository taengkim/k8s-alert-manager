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
from app.models.routing import RoutingMatcher, RoutingRule, routing_rule_channels
from app.models.share import AlertShare
from app.models.silence import SilenceAudit
from app.models.team import Team, TeamLdapMapping, TeamMembership
from app.models.template import MessageTemplate
from app.models.user import User

__all__ = [
    "AlertComment",
    "AlertEvent",
    "AlertShare",
    "AuditLog",
    "Channel",
    "Cluster",
    "MessageTemplate",
    "NotificationOutbox",
    "RoutingMatcher",
    "RoutingRule",
    "SilenceAudit",
    "Team",
    "TeamLdapMapping",
    "TeamMembership",
    "User",
    "routing_rule_channels",
]
