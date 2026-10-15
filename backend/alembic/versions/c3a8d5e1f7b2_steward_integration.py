"""steward integration: run logs, rebuild attempts, audit log

Revision ID: c3a8d5e1f7b2
Revises: 7adf5623a856
Create Date: 2026-10-15 03:00:00.000000

Platform-side tables the Steward subsystem needs (steward/docs/INTEGRATION.md):
  run_logs          the end of a run's output, from the agent's report (FR-GPU-146)
  rebuild_attempts  interim ledger rebuild, attempt state derived from its job (FR-ML-003)
  audit_events      the single append-only audit log (FR-INT-06); a trigger refuses
                    UPDATE and DELETE for every role
The Steward's own tables live in schemas steward / steward_private and are created by
its own migrations (backend/scripts/setup_steward.py).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3a8d5e1f7b2'
down_revision: str | Sequence[str] | None = '7adf5623a856'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('run_logs',
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('log_tail', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('run_id')
    )

    op.create_table('rebuild_attempts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('snapshot_hash', sa.String(length=128), nullable=False),
    sa.Column('project_ref', sa.String(length=255), nullable=True),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id')
    )
    op.create_index('ix_rebuild_attempts_user', 'rebuild_attempts', ['user_id'], unique=False)

    op.create_table('audit_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('actor', sa.String(length=100), nullable=False),
    sa.Column('actor_kind', sa.String(length=10), nullable=False),
    sa.Column('operation', sa.String(length=100), nullable=False),
    sa.Column('resource_ids', sa.ARRAY(sa.String(length=200)), nullable=False),
    sa.Column('outcome', sa.String(length=30), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=True),
    sa.CheckConstraint("actor_kind IN ('user', 'node', 'service')", name='ck_audit_actor_kind'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_events_at', 'audit_events', ['at'], unique=False)
    op.create_index('ix_audit_events_actor', 'audit_events', ['actor'], unique=False)
    op.execute("""
        CREATE FUNCTION audit_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'audit_events is append-only (%)', TG_OP USING ERRCODE = 'restrict_violation';
        END $$;
        CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
          FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION audit_events_append_only()")
    op.drop_index('ix_audit_events_actor', table_name='audit_events')
    op.drop_index('ix_audit_events_at', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index('ix_rebuild_attempts_user', table_name='rebuild_attempts')
    op.drop_table('rebuild_attempts')
    op.drop_table('run_logs')
