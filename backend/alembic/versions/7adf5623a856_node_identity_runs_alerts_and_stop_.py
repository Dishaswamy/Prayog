"""node identity runs alerts and stop requests

Revision ID: 7adf5623a856
Revises: 9d2f61c0b7a4
Create Date: 2026-10-14 15:01:49.863051

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7adf5623a856'
down_revision: Union[str, Sequence[str], None] = '9d2f61c0b7a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_REASONS = "('COMPLETED', 'FAILED', 'WALLTIME_EXCEEDED', 'CANCELLED', 'NODE_FAILURE', 'DISPATCH_TIMEOUT')"
NEW_REASONS = ("('COMPLETED', 'FAILED', 'WALLTIME_EXCEEDED', 'CANCELLED', 'NODE_FAILURE', 'DISPATCH_TIMEOUT', "
               "'MAINTENANCE', 'EMERGENCY_DRAIN')")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('node_alerts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('detail', sa.String(length=255), nullable=False),
    sa.Column('raised_at', sa.DateTime(), nullable=False),
    sa.Column('cleared_at', sa.DateTime(), nullable=True),
    sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
    sa.Column('acknowledged_reason', sa.String(length=255), nullable=True),
    sa.CheckConstraint("kind IN ('UNIT_COUNT_MISMATCH', 'NODE_DOWN', 'UNIT_COUNT_CHANGED')", name='ck_alerts_kind'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    # At most one open alert of each kind per node
    op.create_index('ux_alerts_open', 'node_alerts', ['node_id', 'kind'], unique=True, postgresql_where=sa.text('cleared_at IS NULL AND acknowledged_at IS NULL'))
    op.create_table('runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('allocation_id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('ended_at', sa.DateTime(), nullable=True),
    sa.Column('end_state', sa.String(length=20), nullable=True),
    sa.Column('end_reason', sa.String(length=30), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.CheckConstraint("end_state IN ('COMPLETED', 'FAILED', 'WALLTIME_EXCEEDED', 'CANCELLED', 'INTERRUPTED')", name='ck_runs_end_state'),
    sa.ForeignKeyConstraint(['allocation_id'], ['allocations.id'], ),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('allocation_id')
    )
    op.create_index('ix_runs_job', 'runs', ['job_id'], unique=False)

    op.add_column('allocations', sa.Column('stop_requested_at', sa.DateTime(), nullable=True))
    op.add_column('allocations', sa.Column('stop_reason', sa.String(length=30), nullable=True))
    # Not detected by autogenerate: the release reasons gain MAINTENANCE and EMERGENCY_DRAIN
    op.drop_constraint('ck_alloc_release_reason', 'allocations', type_='check')
    op.create_check_constraint('ck_alloc_release_reason', 'allocations', f"release_reason IN {NEW_REASONS}")
    op.create_check_constraint('ck_alloc_stop_reason', 'allocations', f"stop_reason IN {NEW_REASONS}")
    op.create_check_constraint(
        'ck_alloc_stop_with_reason', 'allocations', "(stop_requested_at IS NULL) = (stop_reason IS NULL)"
    )

    op.add_column('gpu_units', sa.Column('retired_at', sa.DateTime(), nullable=True))
    op.add_column('jobs', sa.Column('dispatch_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('jobs', sa.Column('cancel_reason', sa.String(length=255), nullable=True))
    op.add_column('maintenance_windows', sa.Column('booking_id', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_maintenance_windows_booking_id'), 'maintenance_windows', ['booking_id'], unique=False)

    op.add_column('nodes', sa.Column('reported_unit_count', sa.Integer(), nullable=True))
    op.add_column('nodes', sa.Column('state_changed_at', sa.DateTime(), nullable=True))
    op.add_column('nodes', sa.Column('state_reason', sa.String(length=255), nullable=True))
    op.add_column('nodes', sa.Column('drain_mode', sa.String(length=10), nullable=True))
    op.add_column('nodes', sa.Column('return_requested', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('nodes', sa.Column('enrolment_token_hash', sa.String(length=64), nullable=True))
    op.add_column('nodes', sa.Column('enrolment_expires_at', sa.DateTime(), nullable=True))
    op.add_column('nodes', sa.Column('credential_hash', sa.String(length=64), nullable=True))
    op.add_column('nodes', sa.Column('credential_revoked_at', sa.DateTime(), nullable=True))
    op.add_column('nodes', sa.Column('last_report_seq', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.create_unique_constraint('uq_nodes_credential_hash', 'nodes', ['credential_hash'])
    op.create_check_constraint('ck_nodes_drain_mode', 'nodes', "drain_mode IN ('soft', 'hard')")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_nodes_drain_mode', 'nodes', type_='check')
    op.drop_constraint('uq_nodes_credential_hash', 'nodes', type_='unique')
    op.drop_column('nodes', 'last_report_seq')
    op.drop_column('nodes', 'credential_revoked_at')
    op.drop_column('nodes', 'credential_hash')
    op.drop_column('nodes', 'enrolment_expires_at')
    op.drop_column('nodes', 'enrolment_token_hash')
    op.drop_column('nodes', 'return_requested')
    op.drop_column('nodes', 'drain_mode')
    op.drop_column('nodes', 'state_reason')
    op.drop_column('nodes', 'state_changed_at')
    op.drop_column('nodes', 'reported_unit_count')
    op.drop_index(op.f('ix_maintenance_windows_booking_id'), table_name='maintenance_windows')
    op.drop_column('maintenance_windows', 'booking_id')
    op.drop_column('jobs', 'cancel_reason')
    op.drop_column('jobs', 'dispatch_attempts')
    op.drop_column('gpu_units', 'retired_at')
    op.drop_constraint('ck_alloc_stop_with_reason', 'allocations', type_='check')
    op.drop_constraint('ck_alloc_stop_reason', 'allocations', type_='check')
    op.drop_constraint('ck_alloc_release_reason', 'allocations', type_='check')
    op.create_check_constraint('ck_alloc_release_reason', 'allocations', f"release_reason IN {OLD_REASONS}")
    op.drop_column('allocations', 'stop_reason')
    op.drop_column('allocations', 'stop_requested_at')
    op.drop_index('ix_runs_job', table_name='runs')
    op.drop_table('runs')
    op.drop_index('ux_alerts_open', table_name='node_alerts', postgresql_where=sa.text('cleared_at IS NULL AND acknowledged_at IS NULL'))
    op.drop_table('node_alerts')
