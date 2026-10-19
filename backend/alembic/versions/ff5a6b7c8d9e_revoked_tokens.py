"""revoked tokens (server-side logout)

Revision ID: ff5a6b7c8d9e
Revises: fe4f5a6b7c8d
Create Date: 2026-10-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ff5a6b7c8d9e"
down_revision: Union[str, Sequence[str], None] = "fe4f5a6b7c8d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), server_default=sa.text("(now() AT TIME ZONE 'utc')"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index(op.f("ix_revoked_tokens_user_id"), "revoked_tokens", ["user_id"], unique=False)
    op.create_index(op.f("ix_revoked_tokens_expires_at"), "revoked_tokens", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_revoked_tokens_expires_at"), table_name="revoked_tokens")
    op.drop_index(op.f("ix_revoked_tokens_user_id"), table_name="revoked_tokens")
    op.drop_table("revoked_tokens")
