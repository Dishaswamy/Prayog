"""scheduling passes

Revision ID: 9d2f61c0b7a4
Revises: 438aa63ab7d8
Create Date: 2026-10-13 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d2f61c0b7a4'
down_revision: Union[str, Sequence[str], None] = '438aa63ab7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('scheduling_passes',
    sa.Column('pass_id', sa.String(length=36), nullable=False),
    sa.Column('trigger', sa.String(length=30), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('duration_ms', sa.Double(), nullable=False),
    sa.Column('queued', sa.Integer(), nullable=False),
    sa.Column('placed', sa.Integer(), nullable=False),
    sa.Column('claim_failures', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('pass_id')
    )
    op.create_index('ix_sp_started_at', 'scheduling_passes', ['started_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_sp_started_at', table_name='scheduling_passes')
    op.drop_table('scheduling_passes')
