"""message templates

Revision ID: d665518d2dfb
Revises: 5c0290da23ae
Create Date: 2026-09-23 23:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd665518d2dfb'
down_revision: str | Sequence[str] | None = '5c0290da23ae'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHANNEL_TEMPLATE_FK = 'fk_channels_template_id_message_templates'
_ROUTING_RULE_TEMPLATE_FK = 'fk_routing_rules_template_id_message_templates'


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('message_templates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=1024), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('title_template', sa.Text(), nullable=False),
    sa.Column('body_template', sa.Text(), nullable=False),
    sa.Column('body_html_template', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('team_id', 'name', name='uq_message_template_team_name')
    )

    # batch mode: SQLite can't ALTER TABLE ADD CONSTRAINT directly (it has
    # to copy-and-move the table); batch mode does that on SQLite and falls
    # back to a plain ALTER on dialects (Postgres) that support it natively.
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.add_column(sa.Column('template_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            _CHANNEL_TEMPLATE_FK, 'message_templates', ['template_id'], ['id'], ondelete='SET NULL'
        )

    with op.batch_alter_table('routing_rules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('template_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            _ROUTING_RULE_TEMPLATE_FK, 'message_templates', ['template_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('routing_rules', schema=None) as batch_op:
        batch_op.drop_constraint(_ROUTING_RULE_TEMPLATE_FK, type_='foreignkey')
        batch_op.drop_column('template_id')

    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.drop_constraint(_CHANNEL_TEMPLATE_FK, type_='foreignkey')
        batch_op.drop_column('template_id')

    op.drop_table('message_templates')
