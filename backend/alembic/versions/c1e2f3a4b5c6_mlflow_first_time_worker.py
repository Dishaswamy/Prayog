from alembic import op
import sqlalchemy as sa

revision = 'c1e2f3a4b5c6'
down_revision = '7ab2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('ck_ml_run_status', 'ml_model_runs', type_='check')
    op.create_check_constraint('ck_ml_run_status', 'ml_model_runs', "status IN ('CREATED', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')")
    op.add_column('ml_model_runs', sa.Column('worker_id', sa.String(length=80), nullable=True))
    op.add_column('ml_model_runs', sa.Column('worker_claimed_at', sa.DateTime(), nullable=True))
    op.create_index('ix_ml_runs_worker_claim', 'ml_model_runs', ['status', 'run_type', 'worker_id', 'worker_claimed_at'])


def downgrade():
    op.drop_index('ix_ml_runs_worker_claim', table_name='ml_model_runs')
    op.drop_constraint('ck_ml_run_status', 'ml_model_runs', type_='check')
    op.create_check_constraint('ck_ml_run_status', 'ml_model_runs', "status IN ('CREATED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')")
    op.drop_column('ml_model_runs', 'worker_claimed_at')
    op.drop_column('ml_model_runs', 'worker_id')
