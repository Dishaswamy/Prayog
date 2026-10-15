"""Bind diagnostic links to sessions and audit every authenticated outcome."""

from alembic import op
import sqlalchemy as sa

revision = "f9d0e1f2a3b4"
down_revision = "f8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ml_diagnostic_links", sa.Column("session_hash", sa.String(length=64), nullable=True))
    op.execute("UPDATE ml_diagnostic_links SET session_hash = '' WHERE session_hash IS NULL")
    op.alter_column("ml_diagnostic_links", "session_hash", nullable=False)

    op.create_table(
        "ml_diagnostic_access_audit",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("rebuild_id", sa.Integer(), nullable=True),
        sa.Column("link_id", sa.String(length=36), nullable=True),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'utc')")),
        sa.CheckConstraint(
            "outcome IN ('CREATED', 'CONSUMED', 'INVALID_TTL', 'INVALID_TOKEN', 'EXPIRED', 'REPLAY', 'SESSION_MISMATCH', 'OWNER_MISMATCH', 'ATTEMPT_NOT_FOUND', 'STATE_REJECTED', 'NO_LOG_ARTIFACT', 'ARTIFACT_MISSING', 'ARTIFACT_INTEGRITY_FAILED')",
            name="ck_ml_diagnostic_audit_outcome",
        ),
    )
    op.create_index("ix_ml_diagnostic_audit_user_created", "ml_diagnostic_access_audit", ["user_id", "created_at"])
    op.create_index("ix_ml_diagnostic_audit_attempt_created", "ml_diagnostic_access_audit", ["rebuild_id", "created_at"])


def downgrade():
    op.drop_index("ix_ml_diagnostic_audit_attempt_created", table_name="ml_diagnostic_access_audit")
    op.drop_index("ix_ml_diagnostic_audit_user_created", table_name="ml_diagnostic_access_audit")
    op.drop_table("ml_diagnostic_access_audit")
    op.drop_column("ml_diagnostic_links", "session_hash")
