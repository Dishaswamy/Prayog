from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be blank")
        return value


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    project_id: str
    user_id: int
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Model name cannot be blank")
        return value


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    model_id: str
    project_id: str
    user_id: int
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ExperimentCreate(BaseModel):
    project_id: str = Field(min_length=36, max_length=36)
    model_id: str = Field(min_length=36, max_length=36)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    tags: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Experiment name cannot be blank")
        return value


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: str
    model_id: str
    user_id: int
    name: str
    description: str | None
    tags: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class RunCreate(BaseModel):
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)


class RunStart(BaseModel):
    environment: dict[str, Any] | None = None
    code_artifact_id: str | None = None
    weights_artifact_id: str | None = None


class RunFinish(BaseModel):
    status: Literal["SUCCEEDED", "FAILED"]
    code_artifact_id: str | None = None
    weights_artifact_id: str | None = None
    logs_artifact_id: str | None = None
    error_summary: str | None = Field(default=None, max_length=10000)


class MetricCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    value: float
    step: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Metric name cannot be blank")
        return value


class MetricBatchCreate(BaseModel):
    metrics: list[MetricCreate] = Field(min_length=1, max_length=500)


class MetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int
    user_id: int
    name: str
    value: float
    step: int
    recorded_at: datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    experiment_id: int
    project_id: str
    model_id: str
    user_id: int
    run_type: str
    source_snapshot_id: str | None
    status: str
    logging_state: str
    logging_warnings: list[str]
    hyperparameters: dict[str, Any]
    environment: dict[str, Any]
    code_snapshot_uri: str | None
    code_snapshot_sha256: str | None
    weights_uri: str | None
    logs_uri: str | None
    code_artifact_id: str | None
    weights_artifact_id: str | None
    logs_artifact_id: str | None
    error_summary: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    snapshot_id: str
    experiment_id: int
    project_id: str
    model_id: str
    run_id: int
    user_id: int
    state: str
    code_uri: str | None
    code_sha256: str | None
    dependencies: dict[str, Any]
    hyperparameters: dict[str, Any]
    completeness_reasons: list[str]
    weights_uri: str | None
    code_artifact_id: str | None
    weights_artifact_id: str | None
    logs_artifact_id: str | None
    created_at: datetime


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: int
    experiment_id: int
    project_id: str
    model_id: str
    kind: str
    sha256: str
    size_bytes: int
    created_at: datetime


class RebuildCreate(BaseModel):
    environment: dict[str, Any] = Field(default_factory=dict)


class RebuildUpdate(BaseModel):
    state: Literal["REBUILD_SUCCESS", "REBUILD_FAILED", "REBUILD_CANCELLED"]
    logs_artifact_id: str | None = None
    weights_artifact_id: str | None = None
    failure_summary: str | None = Field(default=None, max_length=10000)


class RebuildOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    snapshot_id: str
    project_id: str
    model_id: str
    execution_run_id: int
    state: str
    environment: dict[str, Any]
    logs_uri: str | None
    logs_artifact_id: str | None
    failure_summary: str | None
    created_at: datetime
    initiated_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class DiagnosticLinkCreate(BaseModel):
    ttl_seconds: int = Field(default=300)


class DiagnosticLinkOut(BaseModel):
    path: str
    expires_at: datetime
