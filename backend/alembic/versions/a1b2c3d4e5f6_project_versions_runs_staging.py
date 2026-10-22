"""project versions, numbered gpu runs, checkpoints, staged datasets

Revision ID: a1b2c3d4e5f6
Revises: fa6b7c8d9e0f
Create Date: 2026-10-22 14:41:58.409218

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'fa6b7c8d9e0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('ml_staged_files',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('blake3', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text("(now() AT TIME ZONE 'utc')"), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint('size_bytes >= 0', name='ck_ml_staged_size'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'blake3', name='uq_ml_staged_user_blake3')
    )
    op.create_table('ml_project_versions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('experiment_id', sa.Integer(), nullable=False),
    sa.Column('version_number', sa.Integer(), nullable=False),
    sa.Column('manifest_hash', sa.String(length=64), nullable=False),
    sa.Column('manifest_artifact_id', sa.String(length=36), nullable=False),
    sa.Column('parent_version_id', sa.Integer(), nullable=True),
    sa.Column('file_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text("(now() AT TIME ZONE 'utc')"), nullable=False),
    sa.CheckConstraint('version_number >= 1', name='ck_ml_version_number'),
    sa.ForeignKeyConstraint(['experiment_id'], ['ml_experiments.id'], ),
    sa.ForeignKeyConstraint(['manifest_artifact_id'], ['ml_artifacts.id'], ),
    sa.ForeignKeyConstraint(['parent_version_id'], ['ml_project_versions.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['ml_projects.project_id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'manifest_hash', name='uq_ml_version_manifest'),
    sa.UniqueConstraint('project_id', 'version_number', name='uq_ml_version_number')
    )
    op.create_table('ml_run_checkpoints',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('artifact_id', sa.String(length=36), nullable=False),
    sa.Column('step', sa.Integer(), nullable=False),
    sa.Column('is_best', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text("(now() AT TIME ZONE 'utc')"), nullable=False),
    sa.CheckConstraint('step >= 0', name='ck_ml_checkpoint_step'),
    sa.ForeignKeyConstraint(['artifact_id'], ['ml_artifacts.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['ml_model_runs.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'step', name='uq_ml_checkpoint_run_step')
    )
    op.create_index('ux_ml_checkpoint_one_best', 'ml_run_checkpoints', ['run_id'], unique=True, postgresql_where=sa.text('is_best'))
    op.create_table('ml_run_dataset_files',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('path', sa.String(length=1024), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('blake3', sa.String(length=64), nullable=False),
    sa.Column('staged_file_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['ml_model_runs.id'], ),
    sa.ForeignKeyConstraint(['staged_file_id'], ['ml_staged_files.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'path', name='uq_ml_run_dataset_path')
    )
    op.add_column('ml_model_runs', sa.Column('project_version_id', sa.Integer(), nullable=True))
    op.add_column('ml_model_runs', sa.Column('run_number', sa.Integer(), nullable=True))
    op.add_column('ml_model_runs', sa.Column('job_id', sa.Integer(), nullable=True))
    op.create_unique_constraint('uq_ml_run_version_number', 'ml_model_runs', ['project_version_id', 'run_number'])
    op.create_unique_constraint('uq_ml_model_runs_job_id', 'ml_model_runs', ['job_id'])
    op.create_foreign_key('fk_ml_model_runs_job_id', 'ml_model_runs', 'jobs', ['job_id'], ['id'])
    op.create_foreign_key('fk_ml_model_runs_project_version_id', 'ml_model_runs', 'ml_project_versions', ['project_version_id'], ['id'])
    op.create_check_constraint(
        'ck_ml_run_numbered_with_version', 'ml_model_runs', '(project_version_id IS NULL) = (run_number IS NULL)'
    )
    op.add_column('ml_projects', sa.Column('current_version_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_ml_projects_current_version', 'ml_projects', 'ml_project_versions', ['current_version_id'], ['id'], ondelete='SET NULL', use_alter=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_ml_projects_current_version', 'ml_projects', type_='foreignkey')
    op.drop_column('ml_projects', 'current_version_id')
    op.drop_constraint('ck_ml_run_numbered_with_version', 'ml_model_runs', type_='check')
    op.drop_constraint('fk_ml_model_runs_project_version_id', 'ml_model_runs', type_='foreignkey')
    op.drop_constraint('fk_ml_model_runs_job_id', 'ml_model_runs', type_='foreignkey')
    op.drop_constraint('uq_ml_model_runs_job_id', 'ml_model_runs', type_='unique')
    op.drop_constraint('uq_ml_run_version_number', 'ml_model_runs', type_='unique')
    op.drop_column('ml_model_runs', 'job_id')
    op.drop_column('ml_model_runs', 'run_number')
    op.drop_column('ml_model_runs', 'project_version_id')
    op.drop_table('ml_run_dataset_files')
    op.drop_index('ux_ml_checkpoint_one_best', table_name='ml_run_checkpoints', postgresql_where=sa.text('is_best'))
    op.drop_table('ml_run_checkpoints')
    op.drop_table('ml_project_versions')
    op.drop_table('ml_staged_files')
