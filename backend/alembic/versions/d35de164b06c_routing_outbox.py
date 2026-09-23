"""routing outbox

Revision ID: d35de164b06c
Revises: 077c0760b984
Create Date: 2026-09-23 19:36:18.407111

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd35de164b06c'
down_revision: str | Sequence[str] | None = '077c0760b984'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUPPRESSED_BY_FK = 'fk_alert_events_suppressed_by_rule_id_routing_rules'


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('routing_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=1024), nullable=True),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('notify_on_firing', sa.Boolean(), nullable=False),
    sa.Column('notify_on_resolved', sa.Boolean(), nullable=False),
    sa.Column('include_shared', sa.Boolean(), nullable=False),
    sa.Column('severities', sa.JSON(), nullable=True),
    sa.Column('namespaces_include', sa.JSON(), nullable=True),
    sa.Column('namespaces_exclude', sa.JSON(), nullable=True),
    sa.Column('clusters', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('team_id', 'name', name='uq_routing_rule_team_name')
    )
    op.create_table('routing_matchers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('routing_rule_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('target', sa.String(length=16), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=True),
    sa.Column('pattern', sa.String(length=512), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['routing_rule_id'], ['routing_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('routing_rule_channels',
    sa.Column('routing_rule_id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['routing_rule_id'], ['routing_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('routing_rule_id', 'channel_id')
    )
    op.create_table('notification_outbox',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('alert_event_id', sa.Integer(), nullable=False),
    sa.Column('routing_rule_id', sa.Integer(), nullable=True),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('locked_by', sa.String(length=64), nullable=True),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['alert_event_id'], ['alert_events.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['routing_rule_id'], ['routing_rules.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('alert_event_id', 'channel_id', 'trigger', name='uq_outbox_event_channel_trigger')
    )
    op.create_index('ix_outbox_status_next_attempt', 'notification_outbox', ['status', 'next_attempt_at'], unique=False)
    # batch mode: SQLite can't ALTER TABLE ADD CONSTRAINT directly (it has
    # to copy-and-move the table); batch mode does that on SQLite and falls
    # back to a plain ALTER on dialects (Postgres) that support it natively.
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('suppressed_by_rule_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            _SUPPRESSED_BY_FK, 'routing_rules', ['suppressed_by_rule_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.drop_constraint(_SUPPRESSED_BY_FK, type_='foreignkey')
        batch_op.drop_column('suppressed_by_rule_id')
    op.drop_index('ix_outbox_status_next_attempt', table_name='notification_outbox')
    op.drop_table('notification_outbox')
    op.drop_table('routing_rule_channels')
    op.drop_table('routing_matchers')
    op.drop_table('routing_rules')
