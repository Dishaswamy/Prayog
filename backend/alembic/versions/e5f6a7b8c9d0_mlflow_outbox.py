from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ml_outbox_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Integer(), sa.ForeignKey("ml_rebuild_attempts.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'utc')")),
        sa.Column("locked_by", sa.String(length=80), nullable=True),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'utc')")),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("event_key", name="uq_ml_outbox_event_key"),
        sa.CheckConstraint("status IN ('PENDING', 'PROCESSING', 'DELIVERED', 'DEAD')", name="ck_ml_outbox_status"),
    )
    op.create_index("ix_ml_outbox_ready", "ml_outbox_events", ["status", "available_at", "locked_until"])


def downgrade():
    op.drop_index("ix_ml_outbox_ready", table_name="ml_outbox_events")
    op.drop_table("ml_outbox_events")
