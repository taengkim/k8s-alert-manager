"""channel soft delete

Revision ID: 1add1fc36d22
Revises: d35de164b06c
Create Date: 2026-09-23 20:25:07.786423

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1add1fc36d22'
down_revision: str | Sequence[str] | None = 'd35de164b06c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch mode: SQLite can't ALTER TABLE ADD COLUMN + DROP CONSTRAINT
    # directly (needs a copy-and-move); falls back to a plain ALTER on
    # dialects (Postgres) that support it natively.
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.drop_constraint('uq_channel_team_name', type_='unique')

    # A partial unique index (CREATE INDEX ... WHERE ...) needs no batch
    # mode -- SQLite supports it directly.
    op.create_index(
        'uq_channel_team_name_active',
        'channels',
        ['team_id', 'name'],
        unique=True,
        sqlite_where=sa.text('deleted_at IS NULL'),
        postgresql_where=sa.text('deleted_at IS NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'uq_channel_team_name_active',
        table_name='channels',
        sqlite_where=sa.text('deleted_at IS NULL'),
        postgresql_where=sa.text('deleted_at IS NULL'),
    )
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.create_unique_constraint('uq_channel_team_name', ['team_id', 'name'])
        batch_op.drop_column('deleted_at')
