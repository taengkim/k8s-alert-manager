"""scheduler escalation renotify retention

Revision ID: 2fda6b281fda
Revises: 81ba106a0a38
Create Date: 2026-09-24 01:40:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2fda6b281fda'
down_revision: str | Sequence[str] | None = '81ba106a0a38'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'scheduled_actions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('alert_event_id', sa.Integer(), nullable=True),
        sa.Column('routing_rule_id', sa.Integer(), nullable=True),
        sa.Column('channel_id', sa.Integer(), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['alert_event_id'], ['alert_events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['routing_rule_id'], ['routing_rules.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_scheduled_actions_status_due', 'scheduled_actions', ['status', 'due_at'], unique=False
    )
    op.create_index(
        'ix_scheduled_actions_event_status',
        'scheduled_actions',
        ['alert_event_id', 'status'],
        unique=False,
    )

    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('value', sa.String(length=255), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )

    op.create_table(
        'routing_rule_escalation_channels',
        sa.Column('routing_rule_id', sa.Integer(), nullable=False),
        sa.Column('channel_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['routing_rule_id'], ['routing_rules.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('routing_rule_id', 'channel_id'),
    )

    with op.batch_alter_table('routing_rules', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('escalation_enabled', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column('escalation_after_minutes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('renotify_interval_minutes', sa.Integer(), nullable=True))

    # Backfill-only default, dropped once existing rows are populated --
    # same convention as alert_events.is_test (see
    # 5c0290da23ae_ack_assignee_comments_test_alert.py).
    with op.batch_alter_table('routing_rules', schema=None) as batch_op:
        batch_op.alter_column('escalation_enabled', server_default=None)

    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'allow_cross_team_escalation', sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.alter_column('allow_cross_team_escalation', server_default=None)

    # Widened from 16 to fit f'renotify:{scheduled_action_id}' (Phase 15) --
    # see app/models/outbox.py's trigger column docstring.
    with op.batch_alter_table('notification_outbox', schema=None) as batch_op:
        batch_op.alter_column(
            'trigger', existing_type=sa.String(length=16), type_=sa.String(length=32)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('notification_outbox', schema=None) as batch_op:
        batch_op.alter_column(
            'trigger', existing_type=sa.String(length=32), type_=sa.String(length=16)
        )

    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.drop_column('allow_cross_team_escalation')

    with op.batch_alter_table('routing_rules', schema=None) as batch_op:
        batch_op.drop_column('renotify_interval_minutes')
        batch_op.drop_column('escalation_after_minutes')
        batch_op.drop_column('escalation_enabled')

    op.drop_table('routing_rule_escalation_channels')
    op.drop_table('app_settings')

    op.drop_index('ix_scheduled_actions_event_status', table_name='scheduled_actions')
    op.drop_index('ix_scheduled_actions_status_due', table_name='scheduled_actions')
    op.drop_table('scheduled_actions')
