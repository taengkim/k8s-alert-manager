"""baseline

Revision ID: 977510a17c8f
Revises: 
Create Date: 2026-09-23 01:23:44.569702

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '977510a17c8f'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
