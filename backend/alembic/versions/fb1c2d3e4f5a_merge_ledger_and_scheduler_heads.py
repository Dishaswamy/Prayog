"""Merge the ML Flow ledger head with the GPU scheduling and Steward head."""

from typing import Sequence, Union

revision: str = "fb1c2d3e4f5a"
down_revision: Union[str, Sequence[str], None] = ("f2a3b4c5d6e7", "c3a8d5e1f7b2")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
