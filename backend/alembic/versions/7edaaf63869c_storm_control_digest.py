"""storm control digest

Revision ID: 7edaaf63869c
Revises: 2fda6b281fda
Create Date: 2026-09-24 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7edaaf63869c'
down_revision: str | Sequence[str] | None = '2fda6b281fda'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rate_limit_per_hour', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('digest_mode', sa.String(length=16), nullable=False, server_default='off')
        )
        batch_op.add_column(
            sa.Column('digest_window_minutes', sa.Integer(), nullable=False, server_default='5')
        )
    # Backfill-only defaults, dropped once existing rows are populated --
    # same convention as escalation_enabled/allow_cross_team_escalation in
    # 2fda6b281fda: the app (Channel model) applies these defaults for new
    # rows going forward, not the DB.
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.alter_column('digest_mode', server_default=None)
        batch_op.alter_column('digest_window_minutes', server_default=None)

    with op.batch_alter_table('notification_outbox', schema=None) as batch_op:
        # Nullable so a digest AGGREGATE row (summarizes many alert events
        # for one channel) can omit it -- a normal row (including a parked
        # one still awaiting its flush) always keeps a real event id. Also
        # reused by Phase 20's reporting work (see this phase's brief).
        # NULL-is-distinct-from-NULL means this never collides with the
        # (alert_event_id, channel_id, trigger) UQ below, which is otherwise
        # left exactly as-is (a parked row still has a real alert_event_id,
        # so it's still fully covered by that UQ).
        batch_op.alter_column(
            'alert_event_id', existing_type=sa.Integer(), nullable=True
        )
        batch_op.add_column(
            sa.Column('is_digest', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column('digested_into_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_notification_outbox_digested_into_id_notification_outbox',
            'notification_outbox',
            ['digested_into_id'],
            ['id'],
            ondelete='SET NULL',
        )
    with op.batch_alter_table('notification_outbox', schema=None) as batch_op:
        batch_op.alter_column('is_digest', server_default=None)

    op.create_index(
        'ix_outbox_channel_digest_created',
        'notification_outbox',
        ['channel_id', 'is_digest', 'created_at'],
        unique=False,
    )
    op.create_index(
        'ix_outbox_channel_status', 'notification_outbox', ['channel_id', 'status'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_outbox_channel_status', table_name='notification_outbox')
    op.drop_index('ix_outbox_channel_digest_created', table_name='notification_outbox')

    with op.batch_alter_table('notification_outbox', schema=None) as batch_op:
        batch_op.drop_constraint(
            'fk_notification_outbox_digested_into_id_notification_outbox', type_='foreignkey'
        )
        batch_op.drop_column('digested_into_id')
        batch_op.drop_column('is_digest')
        batch_op.alter_column('alert_event_id', existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.drop_column('digest_window_minutes')
        batch_op.drop_column('digest_mode')
        batch_op.drop_column('rate_limit_per_hour')
