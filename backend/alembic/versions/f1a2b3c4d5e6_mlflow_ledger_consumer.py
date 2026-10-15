from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("ml_outbox_events", "aggregate_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("ml_outbox_events", sa.Column("run_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_ml_outbox_events_run_id_ml_model_runs",
        "ml_outbox_events",
        "ml_model_runs",
        ["run_id"],
        ["id"],
    )
    op.create_table(
        "ml_event_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'utc')")),
        sa.UniqueConstraint("event_key", name="uq_ml_event_ledger_event_key"),
    )


def downgrade():
    op.drop_table("ml_event_ledger")
    op.drop_constraint("fk_ml_outbox_events_run_id_ml_model_runs", "ml_outbox_events", type_="foreignkey")
    op.drop_column("ml_outbox_events", "run_id")
    op.alter_column("ml_outbox_events", "aggregate_id", existing_type=sa.Integer(), nullable=False)
