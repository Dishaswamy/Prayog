"""platform liveness (defect D3-08)

Revision ID: a9c1e7d3f5b2
Revises: b7d4e2a1c9f0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1e7d3f5b2"
down_revision: Union[str, Sequence[str], None] = "b7d4e2a1c9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_liveness",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("api_seen_at", sa.DateTime(), nullable=False),
        sa.Column("listening_since", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_platform_liveness_one_row"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("platform_liveness")
