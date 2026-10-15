"""Add user- and attempt-scoped Steward context chunks."""

from alembic import op
import sqlalchemy as sa

revision = "f8c9d0e1f2a3"
down_revision = "f7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "steward_context_chunks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("rebuild_id", sa.Integer(), sa.ForeignKey("ml_rebuild_attempts.id"), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'utc')")),
        sa.CheckConstraint(
            "source_type IN ('DIAGNOSTIC_LOG', 'MLFLOW_METADATA', 'EXTERNAL_DOC')",
            name="ck_steward_context_source_type",
        ),
    )
    op.create_index(
        "ix_steward_context_scope",
        "steward_context_chunks",
        ["user_id", "rebuild_id", "created_at"],
    )
    op.create_index(
        "ix_steward_context_source",
        "steward_context_chunks",
        ["user_id", "rebuild_id", "source_type"],
    )


def downgrade():
    op.drop_index("ix_steward_context_source", table_name="steward_context_chunks")
    op.drop_index("ix_steward_context_scope", table_name="steward_context_chunks")
    op.drop_table("steward_context_chunks")
