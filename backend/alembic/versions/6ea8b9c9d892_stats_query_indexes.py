"""stats query indexes

Revision ID: 6ea8b9c9d892
Revises: 7edaaf63869c
Create Date: 2026-09-24 13:14:39.167616

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6ea8b9c9d892'
down_revision: str | Sequence[str] | None = '7edaaf63869c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Phase 19 (app.services.stats): both back range-filtered aggregate
    # queries that would otherwise sequential-scan their table on every
    # Stats page view.
    op.create_index(
        'ix_alert_events_team_starts', 'alert_events', ['team_id', 'starts_at'], unique=False
    )
    op.create_index(
        'ix_outbox_team_status_created',
        'notification_outbox',
        ['team_id', 'status', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_outbox_team_status_created', table_name='notification_outbox')
    op.drop_index('ix_alert_events_team_starts', table_name='alert_events')
