"""Add ML Flow experiment, run, snapshot, metric and rebuild tables.

Revision ID: 3d6a9e4b2c10
Revises: fe89c487563a
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "3d6a9e4b2c10"
down_revision: Union[str, Sequence[str], None] = "fe89c487563a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")


def upgrade() -> None:
    op.create_table(
        "ml_projects",
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_index("ix_ml_projects_user_id", "ml_projects", ["user_id"])
    op.create_index("ix_ml_projects_user_updated", "ml_projects", ["user_id", "updated_at"])

    op.create_table(
        "ml_models",
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["ml_projects.project_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("model_id"),
    )
    op.create_index("ix_ml_models_project_created", "ml_models", ["project_id", "created_at"])

    op.create_table(
        "ml_experiments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["ml_projects.project_id"]),
        sa.ForeignKeyConstraint(["model_id"], ["ml_models.model_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ml_experiments_user_id", "ml_experiments", ["user_id"])
    op.create_index("ix_ml_experiments_user_updated", "ml_experiments", ["user_id", "updated_at"])
    op.create_index("ix_ml_experiments_project_model", "ml_experiments", ["project_id", "model_id"])

    op.create_table(
        "ml_artifacts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("experiment_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("storage_key", sa.String(36), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("kind IN ('CODE', 'WEIGHTS', 'LOG')", name="ck_ml_artifact_kind"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["experiment_id"], ["ml_experiments.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["ml_projects.project_id"]),
        sa.ForeignKeyConstraint(["model_id"], ["ml_models.model_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_ml_artifacts_owner_experiment", "ml_artifacts", ["user_id", "experiment_id"])

    op.create_table(
        "ml_model_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("run_type", sa.String(20), nullable=False),
        sa.Column("source_snapshot_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("logging_state", sa.String(20), nullable=False),
        sa.Column("logging_warnings", sa.JSON(), nullable=False),
        sa.Column("hyperparameters", sa.JSON(), nullable=False),
        sa.Column("environment", sa.JSON(), nullable=False),
        sa.Column("code_snapshot_uri", sa.Text(), nullable=True),
        sa.Column("code_snapshot_sha256", sa.String(64), nullable=True),
        sa.Column("weights_uri", sa.Text(), nullable=True),
        sa.Column("logs_uri", sa.Text(), nullable=True),
        sa.Column("code_artifact_id", sa.String(36), nullable=True),
        sa.Column("weights_artifact_id", sa.String(36), nullable=True),
        sa.Column("logs_artifact_id", sa.String(36), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("status IN ('CREATED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_ml_run_status"),
        sa.CheckConstraint("run_type IN ('FIRST_TIME', 'REBUILD')", name="ck_ml_run_type"),
        sa.CheckConstraint("logging_state IN ('COMPLETE', 'DEGRADED')", name="ck_ml_logging_state"),
        sa.ForeignKeyConstraint(["experiment_id"], ["ml_experiments.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["ml_projects.project_id"]),
        sa.ForeignKeyConstraint(["model_id"], ["ml_models.model_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["code_artifact_id"], ["ml_artifacts.id"]),
        sa.ForeignKeyConstraint(["weights_artifact_id"], ["ml_artifacts.id"]),
        sa.ForeignKeyConstraint(["logs_artifact_id"], ["ml_artifacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ml_runs_user_created", "ml_model_runs", ["user_id", "created_at"])
    op.create_index("ix_ml_runs_experiment_created", "ml_model_runs", ["experiment_id", "created_at"])
    op.create_index("ix_ml_runs_project_created", "ml_model_runs", ["project_id", "created_at"])
    op.create_index("ix_ml_runs_model_created", "ml_model_runs", ["model_id", "created_at"])
    op.create_index("ix_ml_runs_user_status", "ml_model_runs", ["user_id", "status"])

    op.create_table(
        "ml_model_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("experiment_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("code_uri", sa.Text(), nullable=False),
        sa.Column("code_sha256", sa.String(64), nullable=True),
        sa.Column("dependencies", sa.JSON(), nullable=False),
        sa.Column("hyperparameters", sa.JSON(), nullable=False),
        sa.Column("weights_uri", sa.Text(), nullable=True),
        sa.Column("code_artifact_id", sa.String(36), nullable=True),
        sa.Column("weights_artifact_id", sa.String(36), nullable=True),
        sa.Column("logs_artifact_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("state = 'RECORDED'", name="ck_ml_snapshot_state"),
        sa.ForeignKeyConstraint(["code_artifact_id"], ["ml_artifacts.id"]),
        sa.ForeignKeyConstraint(["weights_artifact_id"], ["ml_artifacts.id"]),
        sa.ForeignKeyConstraint(["logs_artifact_id"], ["ml_artifacts.id"]),
        sa.ForeignKeyConstraint(["experiment_id"], ["ml_experiments.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["ml_projects.project_id"]),
        sa.ForeignKeyConstraint(["model_id"], ["ml_models.model_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["ml_model_runs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_ml_snapshots_user_created", "ml_model_snapshots", ["user_id", "created_at"])
    op.create_index("ix_ml_snapshots_experiment_created", "ml_model_snapshots", ["experiment_id", "created_at"])
    op.create_index("ix_ml_snapshots_project_created", "ml_model_snapshots", ["project_id", "created_at"])
    op.create_index("ix_ml_snapshots_model_created", "ml_model_snapshots", ["model_id", "created_at"])

    op.create_table(
        "ml_run_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("step >= 0", name="ck_ml_metric_step"),
        sa.ForeignKeyConstraint(["run_id"], ["ml_model_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "name", "step", name="uq_ml_metric_run_name_step"),
    )
    op.create_index("ix_ml_metrics_run_recorded", "ml_run_metrics", ["run_id", "recorded_at"])

    op.create_table(
        "ml_rebuild_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("execution_run_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("environment", sa.JSON(), nullable=False),
        sa.Column("logs_uri", sa.Text(), nullable=True),
        sa.Column("logs_artifact_id", sa.String(36), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column("initiated_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.CheckConstraint("state IN ('REBUILD_INITIATED', 'REBUILD_SUCCESS', 'REBUILD_FAILED', 'REBUILD_CANCELLED')", name="ck_ml_rebuild_state"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["ml_model_snapshots.snapshot_id"]),
        sa.ForeignKeyConstraint(["project_id"], ["ml_projects.project_id"]),
        sa.ForeignKeyConstraint(["model_id"], ["ml_models.model_id"]),
        sa.ForeignKeyConstraint(["execution_run_id"], ["ml_model_runs.id"]),
        sa.ForeignKeyConstraint(["logs_artifact_id"], ["ml_artifacts.id"]),
        sa.UniqueConstraint("execution_run_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ml_rebuild_user_state", "ml_rebuild_attempts", ["user_id", "state"])
    op.create_index("ix_ml_rebuild_snapshot_created", "ml_rebuild_attempts", ["snapshot_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ml_rebuild_snapshot_created", table_name="ml_rebuild_attempts")
    op.drop_index("ix_ml_rebuild_user_state", table_name="ml_rebuild_attempts")
    op.drop_table("ml_rebuild_attempts")
    op.drop_index("ix_ml_metrics_run_recorded", table_name="ml_run_metrics")
    op.drop_table("ml_run_metrics")
    op.drop_index("ix_ml_snapshots_model_created", table_name="ml_model_snapshots")
    op.drop_index("ix_ml_snapshots_project_created", table_name="ml_model_snapshots")
    op.drop_index("ix_ml_snapshots_experiment_created", table_name="ml_model_snapshots")
    op.drop_index("ix_ml_snapshots_user_created", table_name="ml_model_snapshots")
    op.drop_table("ml_model_snapshots")
    op.drop_index("ix_ml_runs_user_status", table_name="ml_model_runs")
    op.drop_index("ix_ml_runs_model_created", table_name="ml_model_runs")
    op.drop_index("ix_ml_runs_project_created", table_name="ml_model_runs")
    op.drop_index("ix_ml_runs_experiment_created", table_name="ml_model_runs")
    op.drop_index("ix_ml_runs_user_created", table_name="ml_model_runs")
    op.drop_table("ml_model_runs")
    op.drop_index("ix_ml_artifacts_owner_experiment", table_name="ml_artifacts")
    op.drop_table("ml_artifacts")
    op.drop_index("ix_ml_experiments_project_model", table_name="ml_experiments")
    op.drop_index("ix_ml_experiments_user_updated", table_name="ml_experiments")
    op.drop_index("ix_ml_experiments_user_id", table_name="ml_experiments")
    op.drop_table("ml_experiments")
    op.drop_index("ix_ml_models_project_created", table_name="ml_models")
    op.drop_table("ml_models")
    op.drop_index("ix_ml_projects_user_updated", table_name="ml_projects")
    op.drop_index("ix_ml_projects_user_id", table_name="ml_projects")
    op.drop_table("ml_projects")
