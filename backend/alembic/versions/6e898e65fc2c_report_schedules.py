"""report schedules

Revision ID: 6e898e65fc2c
Revises: 6ea8b9c9d892
Create Date: 2026-09-24 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6e898e65fc2c'
down_revision: str | Sequence[str] | None = '6ea8b9c9d892'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('report_schedules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('cadence', sa.String(length=16), nullable=False),
    sa.Column('weekday', sa.Integer(), nullable=True),
    sa.Column('hour', sa.Integer(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('template_id', sa.Integer(), nullable=True),
    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_status', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['template_id'], ['message_templates.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        'ix_report_schedules_enabled_next_run', 'report_schedules', ['enabled', 'next_run_at'], unique=False
    )
    op.create_table('report_schedule_channels',
    sa.Column('report_schedule_id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['report_schedule_id'], ['report_schedules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('report_schedule_id', 'channel_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('report_schedule_channels')
    op.drop_index('ix_report_schedules_enabled_next_run', table_name='report_schedules')
    op.drop_table('report_schedules')
