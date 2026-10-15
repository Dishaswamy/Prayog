"""Use BLAKE3 content addresses for ML Flow artifact objects.

Revision ID: f2a3b4c5d6e7
Revises: e5f6a7b8c9d0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "f9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing UUID keys remain valid. New rows use a 64-character BLAKE3 hex digest.
    # Multiple artifact metadata rows may reference one immutable content object.
    op.drop_constraint("ml_artifacts_storage_key_key", "ml_artifacts", type_="unique")
    op.alter_column(
        "ml_artifacts",
        "storage_key",
        existing_type=sa.String(length=36),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_index(
        "ix_ml_artifacts_storage_key",
        "ml_artifacts",
        ["storage_key"],
        unique=False,
    )


def downgrade() -> None:
    # A downgrade is only safe when no two metadata rows share a content key and
    # every key fits the old UUID-width column.
    op.drop_index("ix_ml_artifacts_storage_key", table_name="ml_artifacts")
    op.alter_column(
        "ml_artifacts",
        "storage_key",
        existing_type=sa.String(length=64),
        type_=sa.String(length=36),
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "ml_artifacts_storage_key_key",
        "ml_artifacts",
        ["storage_key"],
    )
