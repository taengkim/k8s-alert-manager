"""alert shares

Revision ID: 81ba106a0a38
Revises: d665518d2dfb
Create Date: 2026-09-24 00:26:24.262890

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '81ba106a0a38'
down_revision: str | Sequence[str] | None = 'd665518d2dfb'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('alert_shares',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('owner_team_id', sa.Integer(), nullable=False),
    sa.Column('target_team_id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('matchers', sa.JSON(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('owner_team_id != target_team_id', name='ck_alert_share_owner_ne_target'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_team_id', 'target_team_id', name='uq_alert_share_owner_target')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('alert_shares')
