from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ml_diagnostic_links",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("rebuild_id", sa.Integer(), sa.ForeignKey("ml_rebuild_attempts.id"), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), sa.ForeignKey("ml_artifacts.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(now() AT TIME ZONE 'utc')"), nullable=False),
    )
    op.create_index("ix_ml_diagnostic_links_expiry", "ml_diagnostic_links", ["expires_at"])
    op.create_index("ix_ml_diagnostic_links_user_created", "ml_diagnostic_links", ["user_id", "created_at"])


def downgrade():
    op.drop_index("ix_ml_diagnostic_links_user_created", table_name="ml_diagnostic_links")
    op.drop_index("ix_ml_diagnostic_links_expiry", table_name="ml_diagnostic_links")
    op.drop_table("ml_diagnostic_links")
