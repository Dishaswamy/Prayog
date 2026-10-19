"""one project name per user (case-insensitive)

Revision ID: fa6b7c8d9e0f
Revises: ff5a6b7c8d9e
Create Date: 2026-10-19

The CLI looks projects up by name, so a user's project names must be distinct. Existing duplicates
(same user, same name ignoring case) are kept but renamed "name (2)", "name (3)", ... oldest first
keeping its name, so the unique index can be created without deleting anything.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "fa6b7c8d9e0f"
down_revision: Union[str, Sequence[str], None] = "ff5a6b7c8d9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE ml_projects p
        SET name = left(p.name, 110) || ' (' || d.rn || ')'
        FROM (
            SELECT project_id,
                   row_number() OVER (PARTITION BY user_id, lower(name) ORDER BY created_at, project_id) AS rn
            FROM ml_projects
        ) d
        WHERE p.project_id = d.project_id AND d.rn > 1
    """)
    op.execute("CREATE UNIQUE INDEX uq_ml_projects_user_lower_name ON ml_projects (user_id, lower(name))")


def downgrade() -> None:
    op.execute("DROP INDEX uq_ml_projects_user_lower_name")
