"""Add worker claim fields to rebuild attempts."""
from alembic import op
import sqlalchemy as sa

revision = "7ab2c3d4e5f6"
down_revision = "3d6a9e4b2c10"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ml_rebuild_attempts", sa.Column("worker_id", sa.String(length=80), nullable=True))
    op.add_column("ml_rebuild_attempts", sa.Column("worker_claimed_at", sa.DateTime(), nullable=True))
    op.create_index("ix_ml_rebuild_worker_claim", "ml_rebuild_attempts", ["state", "worker_id", "worker_claimed_at"])


def downgrade():
    op.drop_index("ix_ml_rebuild_worker_claim", table_name="ml_rebuild_attempts")
    op.drop_column("ml_rebuild_attempts", "worker_claimed_at")
    op.drop_column("ml_rebuild_attempts", "worker_id")
