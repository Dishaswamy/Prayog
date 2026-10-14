"""release reason and charged usage

Revision ID: 438aa63ab7d8
Revises: 2c898af77bf1
Create Date: 2026-10-13 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '438aa63ab7d8'
down_revision: Union[str, Sequence[str], None] = '2c898af77bf1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RELEASE_REASONS = "('COMPLETED', 'FAILED', 'WALLTIME_EXCEEDED', 'CANCELLED', 'NODE_FAILURE', 'DISPATCH_TIMEOUT')"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('allocations', sa.Column('release_reason', sa.String(length=30), nullable=True))
    op.create_check_constraint('ck_alloc_release_reason', 'allocations', f"release_reason IN {RELEASE_REASONS}")
    op.create_check_constraint(
        'ck_alloc_released_with_reason', 'allocations', "(released_at IS NULL) = (release_reason IS NULL)"
    )
    op.add_column(
        'usage_entries', sa.Column('charged', sa.Boolean(), server_default=sa.text('true'), nullable=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('usage_entries', 'charged')
    op.drop_constraint('ck_alloc_released_with_reason', 'allocations', type_='check')
    op.drop_constraint('ck_alloc_release_reason', 'allocations', type_='check')
    op.drop_column('allocations', 'release_reason')
