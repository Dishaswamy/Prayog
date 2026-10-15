"""Merge the run-sealed ledger and incomplete-snapshot schema heads."""

from typing import Sequence, Union

revision: str = "fa0b1c2d3e4f"
down_revision: Union[str, Sequence[str], None] = ("f1a2b3c4d5e6", "f6a7b8c9d0e1")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
