"""add mlflow projects and runs

Revision ID: 8c4a7f21d6b9
Revises: 02e8c3adc41f
Create Date: 2026-10-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "8c4a7f21d6b9"
down_revision: Union[str, Sequence[str], None] = "02e8c3adc41f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create MLFlow project and run tables."""
    op.create_table(
        "mlflow_projects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("project_path", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "mlflow_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="CREATED",
        ),
        sa.Column("environment", postgresql.JSONB(), nullable=True),
        sa.Column("hyperparameters", postgresql.JSONB(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("gpu_details", postgresql.JSONB(), nullable=True),
        sa.Column("results", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["mlflow_projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop MLFlow project and run tables."""
    op.drop_table("mlflow_runs")
    op.drop_table("mlflow_projects")
