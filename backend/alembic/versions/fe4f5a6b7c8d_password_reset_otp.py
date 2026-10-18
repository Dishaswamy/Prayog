"""password reset otp

Revision ID: fe4f5a6b7c8d
Revises: fd3e4f5a6b7c
Create Date: 2026-10-18

Ported from the otp-reset branch onto the current migration chain.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "fe4f5a6b7c8d"
down_revision: Union[str, Sequence[str], None] = "fd3e4f5a6b7c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "password_reset_otps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("otp_hash", sa.String(length=64), nullable=False),
        sa.Column("new_password_hash", sa.String(length=255), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(now() AT TIME ZONE 'utc')"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_password_reset_otps_user_id"), "password_reset_otps", ["user_id"], unique=False)
    op.add_column("users", sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False))


def downgrade() -> None:
    op.drop_column("users", "token_version")
    op.drop_index(op.f("ix_password_reset_otps_user_id"), table_name="password_reset_otps")
    op.drop_table("password_reset_otps")
