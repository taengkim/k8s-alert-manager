"""SQLAlchemy models.

Import every model module here so `Base.metadata` is fully populated for
`Base.metadata.create_all(...)` (tests) and Alembic autogenerate.
"""

from app.models.audit import AuditLog
from app.models.cluster import Cluster
from app.models.silence import SilenceAudit
from app.models.team import Team, TeamLdapMapping, TeamMembership
from app.models.user import User

__all__ = [
    "AuditLog",
    "Cluster",
    "SilenceAudit",
    "Team",
    "TeamLdapMapping",
    "TeamMembership",
    "User",
]
