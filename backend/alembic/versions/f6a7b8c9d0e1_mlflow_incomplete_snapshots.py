from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_ml_snapshot_state", "ml_model_snapshots", type_="check")
    op.create_check_constraint(
        "ck_ml_snapshot_state",
        "ml_model_snapshots",
        "state IN ('RECORDED', 'INCOMPLETE')",
    )
    op.alter_column("ml_model_snapshots", "code_uri", existing_type=sa.Text(), nullable=True)
    op.add_column(
        "ml_model_snapshots",
        sa.Column("completeness_reasons", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM ml_model_snapshots WHERE state = 'INCOMPLETE') THEN
                RAISE EXCEPTION 'Cannot downgrade while INCOMPLETE snapshots exist; export or remove them first';
            END IF;
        END $$;
    """)
    op.drop_column("ml_model_snapshots", "completeness_reasons")
    op.alter_column("ml_model_snapshots", "code_uri", existing_type=sa.Text(), nullable=False)
    op.drop_constraint("ck_ml_snapshot_state", "ml_model_snapshots", type_="check")
    op.create_check_constraint("ck_ml_snapshot_state", "ml_model_snapshots", "state = 'RECORDED'")
