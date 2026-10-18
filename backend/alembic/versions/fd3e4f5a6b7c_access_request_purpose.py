"""access request purpose

Revision ID: fd3e4f5a6b7c
Revises: fc2d3e4f5a6b
Create Date: 2026-10-18

Staged: add nullable, backfill, then enforce. Researchers are always
RESEARCH_PROJECT. A student's purpose cannot be inferred, so existing student
requests must be classified by hand before this migration can finish.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "fd3e4f5a6b7c"
down_revision: Union[str, Sequence[str], None] = "fc2d3e4f5a6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("professors", sa.Column("lab_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_professors_lab_id", "professors", "labs", ["lab_id"], ["id"])
    op.add_column("access_requests", sa.Column("revoke_requested_at", sa.DateTime(), nullable=True))
    op.add_column("access_requests", sa.Column("revoked_at", sa.DateTime(), nullable=True))
    op.add_column("access_requests", sa.Column("revoke_reason", sa.String(length=20), nullable=True))
    op.execute("UPDATE access_requests SET revoke_reason = 'ADMIN', revoked_at = updated_at WHERE final_status = 'REVOKED'")
    op.create_check_constraint(
        "ck_ar_revoke_reason", "access_requests", "revoke_reason IS NULL OR revoke_reason IN ('ADMIN', 'EXPIRED')"
    )
    op.add_column("access_requests", sa.Column("purpose", sa.String(length=30), nullable=True))
    op.execute("UPDATE access_requests SET purpose = 'RESEARCH_PROJECT' WHERE role = 'RESEARCHER'")

    unclassified = op.get_bind().execute(
        sa.text("SELECT count(*) FROM access_requests WHERE purpose IS NULL")
    ).scalar()
    if unclassified:
        raise RuntimeError(
            f"{unclassified} existing student access request(s) have no purpose. Set it by hand "
            "first: UPDATE access_requests SET purpose = 'COURSE_PROJECT' (or 'RESEARCH_PROJECT') "
            "WHERE id IN (...), then rerun the migration."
        )

    op.alter_column("access_requests", "purpose", nullable=False)
    op.create_check_constraint(
        "ck_ar_purpose", "access_requests", "purpose IN ('COURSE_PROJECT', 'RESEARCH_PROJECT')"
    )
    op.create_check_constraint(
        "ck_ar_purpose_role", "access_requests", "role = 'STUDENT' OR purpose = 'RESEARCH_PROJECT'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_ar_purpose_role", "access_requests", type_="check")
    op.drop_constraint("ck_ar_purpose", "access_requests", type_="check")
    op.drop_column("access_requests", "purpose")
    op.drop_constraint("ck_ar_revoke_reason", "access_requests", type_="check")
    op.drop_column("access_requests", "revoke_reason")
    op.drop_column("access_requests", "revoked_at")
    op.drop_column("access_requests", "revoke_requested_at")
    op.drop_constraint("fk_professors_lab_id", "professors", type_="foreignkey")
    op.drop_column("professors", "lab_id")
