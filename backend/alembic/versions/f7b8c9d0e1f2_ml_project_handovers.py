"""Add transactional ML project IP handover requests."""

from alembic import op
import sqlalchemy as sa

revision = "f7b8c9d0e1f2"
down_revision = "fa0b1c2d3e4f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ml_project_handovers",
        sa.Column("handover_id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("ml_projects.project_id"), nullable=False),
        sa.Column("source_owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("target_professor_id", sa.Integer(), sa.ForeignKey("professors.id"), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("artifact_inventory", sa.JSON(), nullable=False),
        sa.Column("failed_artifacts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'utc')")),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('HANDOVER_PENDING', 'HANDOVER_APPROVED', 'HANDOVER_REJECTED', 'HANDOVER_CANCELLED')",
            name="ck_ml_handover_status",
        ),
    )
    op.create_index("ix_ml_handover_target_status", "ml_project_handovers", ["target_professor_id", "status"])
    op.create_index("ix_ml_handover_project_created", "ml_project_handovers", ["project_id", "created_at"])


def downgrade():
    op.drop_index("ix_ml_handover_project_created", table_name="ml_project_handovers")
    op.drop_index("ix_ml_handover_target_status", table_name="ml_project_handovers")
    op.drop_table("ml_project_handovers")
