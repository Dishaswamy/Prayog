from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from models import Base

UTC_NOW = text("(now() AT TIME ZONE 'utc')")


class Project(Base):
    __tablename__ = "ml_projects"
    __table_args__ = (Index("ix_ml_projects_user_updated", "user_id", "updated_at"),)

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, onupdate=func.timezone("utc", func.now()), nullable=False)


class MLModel(Base):
    __tablename__ = "ml_models"
    __table_args__ = (Index("ix_ml_models_project_created", "project_id", "created_at"),)

    model_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("ml_projects.project_id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, onupdate=func.timezone("utc", func.now()), nullable=False)


class Experiment(Base):
    __tablename__ = "ml_experiments"
    __table_args__ = (
        Index("ix_ml_experiments_user_updated", "user_id", "updated_at"),
        Index("ix_ml_experiments_project_model", "project_id", "model_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("ml_projects.project_id"), nullable=False)
    model_id: Mapped[str] = mapped_column(ForeignKey("ml_models.model_id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, onupdate=func.timezone("utc", func.now()), nullable=False)


class ModelRun(Base):
    __tablename__ = "ml_model_runs"
    __table_args__ = (
        CheckConstraint("status IN ('CREATED', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_ml_run_status"),
        CheckConstraint("run_type IN ('FIRST_TIME', 'REBUILD')", name="ck_ml_run_type"),
        CheckConstraint("logging_state IN ('COMPLETE', 'DEGRADED')", name="ck_ml_logging_state"),
        Index("ix_ml_runs_user_created", "user_id", "created_at"),
        Index("ix_ml_runs_experiment_created", "experiment_id", "created_at"),
        Index("ix_ml_runs_project_created", "project_id", "created_at"),
        Index("ix_ml_runs_model_created", "model_id", "created_at"),
        Index("ix_ml_runs_user_status", "user_id", "status"),
        Index("ix_ml_runs_worker_claim", "status", "run_type", "worker_id", "worker_claimed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("ml_experiments.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("ml_projects.project_id"), nullable=False)
    model_id: Mapped[str] = mapped_column(ForeignKey("ml_models.model_id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    run_type: Mapped[str] = mapped_column(String(20), nullable=False, default="FIRST_TIME")
    source_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="CREATED")
    logging_state: Mapped[str] = mapped_column(String(20), nullable=False, default="COMPLETE")
    logging_warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    hyperparameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    environment: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    code_snapshot_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_snapshot_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    weights_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.id"), nullable=True)
    weights_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.id"), nullable=True)
    logs_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.id"), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    worker_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, onupdate=func.timezone("utc", func.now()), nullable=False)


class Artifact(Base):
    __tablename__ = "ml_artifacts"
    __table_args__ = (
        CheckConstraint("kind IN ('CODE', 'WEIGHTS', 'LOG')", name="ck_ml_artifact_kind"),
        Index("ix_ml_artifacts_owner_experiment", "user_id", "experiment_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("ml_experiments.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("ml_projects.project_id"), nullable=False)
    model_id: Mapped[str] = mapped_column(ForeignKey("ml_models.model_id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)


class ModelSnapshot(Base):
    __tablename__ = "ml_model_snapshots"
    __table_args__ = (
        CheckConstraint("state IN ('RECORDED', 'INCOMPLETE')", name="ck_ml_snapshot_state"),
        Index("ix_ml_snapshots_user_created", "user_id", "created_at"),
        Index("ix_ml_snapshots_experiment_created", "experiment_id", "created_at"),
        Index("ix_ml_snapshots_project_created", "project_id", "created_at"),
        Index("ix_ml_snapshots_model_created", "model_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("ml_experiments.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("ml_projects.project_id"), nullable=False)
    model_id: Mapped[str] = mapped_column(ForeignKey("ml_models.model_id"), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("ml_model_runs.id"), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="RECORDED")
    code_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependencies: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    hyperparameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    completeness_reasons: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'::json"), nullable=False)
    weights_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.id"), nullable=True)
    weights_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.id"), nullable=True)
    logs_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)


class RunMetric(Base):
    __tablename__ = "ml_run_metrics"
    __table_args__ = (
        CheckConstraint("step >= 0", name="ck_ml_metric_step"),
        Index("ix_ml_metrics_run_recorded", "run_id", "recorded_at"),
        UniqueConstraint("run_id", "name", "step", name="uq_ml_metric_run_name_step"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ml_model_runs.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)


class RebuildAttempt(Base):
    __tablename__ = "ml_rebuild_attempts"
    __table_args__ = (
        CheckConstraint("state IN ('REBUILD_INITIATED', 'REBUILD_SUCCESS', 'REBUILD_FAILED', 'REBUILD_CANCELLED')", name="ck_ml_rebuild_state"),
        Index("ix_ml_rebuild_user_state", "user_id", "state"),
        Index("ix_ml_rebuild_snapshot_created", "snapshot_id", "created_at"),
        Index("ix_ml_rebuild_worker_claim", "state", "worker_id", "worker_claimed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("ml_model_snapshots.snapshot_id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("ml_projects.project_id"), nullable=False)
    model_id: Mapped[str] = mapped_column(ForeignKey("ml_models.model_id"), nullable=False)
    execution_run_id: Mapped[int] = mapped_column(ForeignKey("ml_model_runs.id"), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="REBUILD_INITIATED")
    environment: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    logs_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.id"), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    worker_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, onupdate=func.timezone("utc", func.now()), nullable=False)


class MLFlowOutboxEvent(Base):
    __tablename__ = "ml_outbox_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_ml_outbox_event_key"),
        CheckConstraint("status IN ('PENDING', 'PROCESSING', 'DELIVERED', 'DEAD')", name="ck_ml_outbox_status"),
        Index("ix_ml_outbox_ready", "status", "available_at", "locked_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[int | None] = mapped_column(ForeignKey("ml_rebuild_attempts.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("ml_model_runs.id"), nullable=True)
    user_id = mapped_column(ForeignKey("users.id"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MLFlowLedgerEntry(Base):
    __tablename__ = "ml_event_ledger"
    __table_args__ = (UniqueConstraint("event_key", name="uq_ml_event_ledger_event_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)


class DiagnosticLink(Base):
    __tablename__ = "ml_diagnostic_links"
    __table_args__ = (
        Index("ix_ml_diagnostic_links_expiry", "expires_at"),
        Index("ix_ml_diagnostic_links_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    rebuild_id: Mapped[int] = mapped_column(ForeignKey("ml_rebuild_attempts.id"), nullable=False)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("ml_artifacts.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)


class ProjectHandover(Base):
    __tablename__ = "ml_project_handovers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('HANDOVER_PENDING', 'HANDOVER_APPROVED', 'HANDOVER_REJECTED', 'HANDOVER_CANCELLED')",
            name="ck_ml_handover_status",
        ),
        Index("ix_ml_handover_target_status", "target_professor_id", "status"),
        Index("ix_ml_handover_project_created", "project_id", "created_at"),
    )

    handover_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("ml_projects.project_id"), nullable=False)
    source_owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="HANDOVER_PENDING")
    artifact_inventory: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    failed_artifacts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
