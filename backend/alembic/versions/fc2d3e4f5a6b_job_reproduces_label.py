"""A rebuild's job carries the run it reproduces (PRAYOG-91; §14.3, ADR-08)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "fc2d3e4f5a6b"
down_revision: Union[str, Sequence[str], None] = "fb1c2d3e4f5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("reproduces", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "reproduces")
