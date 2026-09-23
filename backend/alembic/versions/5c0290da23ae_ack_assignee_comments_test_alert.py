"""ack assignee comments test alert

Revision ID: 5c0290da23ae
Revises: 1add1fc36d22
Create Date: 2026-09-23 21:05:56.673319

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5c0290da23ae'
down_revision: str | Sequence[str] | None = '1add1fc36d22'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACKNOWLEDGED_BY_FK = 'fk_alert_events_acknowledged_by_users'
_ASSIGNEE_FK = 'fk_alert_events_assignee_user_id_users'


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('alert_comments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('alert_event_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['alert_event_id'], ['alert_events.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        'ix_alert_comments_event_created', 'alert_comments', ['alert_event_id', 'created_at'],
        unique=False,
    )

    # batch mode: SQLite can't ALTER TABLE ADD CONSTRAINT directly (it has
    # to copy-and-move the table); batch mode does that on SQLite and falls
    # back to a plain ALTER on dialects (Postgres) that support it natively.
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('acknowledged_by', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('assignee_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('is_test', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_foreign_key(
            _ACKNOWLEDGED_BY_FK, 'users', ['acknowledged_by'], ['id'], ondelete='SET NULL'
        )
        batch_op.create_foreign_key(
            _ASSIGNEE_FK, 'users', ['assignee_user_id'], ['id'], ondelete='SET NULL'
        )

    # The server_default above exists only to backfill pre-existing rows
    # (SQLite/Postgres both require a default for a NOT NULL column added to
    # a non-empty table); the ORM column has no such default going forward,
    # so it's dropped once the column is populated -- consistent with every
    # other boolean column in this schema (e.g. Channel.enabled).
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.alter_column('is_test', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.drop_constraint(_ASSIGNEE_FK, type_='foreignkey')
        batch_op.drop_constraint(_ACKNOWLEDGED_BY_FK, type_='foreignkey')
        batch_op.drop_column('is_test')
        batch_op.drop_column('assignee_user_id')
        batch_op.drop_column('acknowledged_by')
        batch_op.drop_column('acknowledged_at')

    op.drop_index('ix_alert_comments_event_created', table_name='alert_comments')
    op.drop_table('alert_comments')
