from datetime import datetime
import re
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


class ProfessorProjectOut(BaseModel):
    """Professor project metadata; excludes the owner's internal user ID."""

    model_config = ConfigDict(from_attributes=True)
    project_id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime

    @field_validator("name", "description", mode="before")
    @classmethod
    def redact_project_text(cls, value):
        return _redact_sensitive_text(value)



class ProfessorModelOut(BaseModel):
    """Professor model metadata; excludes the owner's internal user ID."""

    model_config = ConfigDict(from_attributes=True)
    model_id: str
    project_id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime

    @field_validator("name", "description", mode="before")
    @classmethod
    def redact_model_text(cls, value):
        return _redact_sensitive_text(value)



class ProfessorExperimentOut(BaseModel):
    """Professor experiment metadata with credential-like fields redacted."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: str
    model_id: str
    name: str
    description: str | None
    tags: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @field_validator("tags", mode="before")
    @classmethod
    def redact_experiment_tags(cls, value):
        return _redact_sensitive_metadata(value)

    @field_validator("name", "description", mode="before")
    @classmethod
    def redact_experiment_text(cls, value):
        return _redact_sensitive_text(value)



class ProfessorRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    experiment_id: int
    project_id: str
    model_id: str
    run_type: str
    source_snapshot_id: str | None
    status: str
    logging_state: str
    logging_warnings: list[str]
    hyperparameters: dict[str, Any]
    environment: dict[str, Any]
    code_snapshot_sha256: str | None
    code_artifact_id: str | None
    weights_artifact_id: str | None
    logs_artifact_id: str | None
    error_summary: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime

    @field_validator("hyperparameters", "environment", mode="before")
    @classmethod
    def redact_run_metadata(cls, value):
        return _redact_sensitive_metadata(value)

    @field_validator("error_summary", mode="before")
    @classmethod
    def redact_run_error_summary(cls, value):
        return _redact_sensitive_text(value)

    @field_validator("logging_warnings", mode="before")
    @classmethod
    def redact_run_logging_warnings(cls, value):
        return [_redact_sensitive_text(item) for item in value] if isinstance(value, list) else value



class ProfessorSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    snapshot_id: str
    experiment_id: int
    project_id: str
    model_id: str
    run_id: int
    state: str
    code_sha256: str | None
    dependencies: dict[str, Any]
    hyperparameters: dict[str, Any]
    completeness_reasons: list[str] = Field(default_factory=list)
    code_artifact_id: str | None
    weights_artifact_id: str | None
    logs_artifact_id: str | None
    created_at: datetime

    @field_validator("dependencies", "hyperparameters", mode="before")
    @classmethod
    def redact_snapshot_metadata(cls, value):
        return _redact_sensitive_metadata(value)

    @field_validator("completeness_reasons", mode="before")
    @classmethod
    def redact_completeness_reasons(cls, value):
        return [_redact_sensitive_text(item) for item in value] if isinstance(value, list) else value



class ProfessorRebuildOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    snapshot_id: str
    project_id: str
    model_id: str
    execution_run_id: int
    state: str
    environment: dict[str, Any]
    logs_artifact_id: str | None
    failure_summary: str | None
    created_at: datetime
    initiated_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime

    @field_validator("environment", mode="before")
    @classmethod
    def redact_rebuild_metadata(cls, value):
        return _redact_sensitive_metadata(value)

    @field_validator("failure_summary", mode="before")
    @classmethod
    def redact_rebuild_failure_summary(cls, value):
        return _redact_sensitive_text(value)


class ProfessorMetricOut(BaseModel):
    """Professor metric view without the owner's internal user ID."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int
    name: str
    value: float
    step: int
    recorded_at: datetime



class ProfessorArtifactOut(BaseModel):
    """Explicit allowlist for Professor artifact inventory; never includes storage locators."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    experiment_id: int
    project_id: str
    model_id: str
    kind: str
    sha256: str
    size_bytes: int
    created_at: datetime



_SENSITIVE_METADATA_KEY_PARTS = (
    "password", "secret", "token", "credential", "api_key", "apikey",
    "access_key", "private_key", "authorization", "cookie",
)


def _redact_sensitive_metadata(value):
    """Redact obvious credential-bearing keys in user-supplied JSON metadata."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_METADATA_KEY_PARTS):
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = _redact_sensitive_metadata(item)
        return cleaned
    if isinstance(value, list):
        return [_redact_sensitive_metadata(item) for item in value]
    if isinstance(value, str):
        # Metadata occasionally contains serialized diagnostic fragments rather
        # than a dedicated credential key. Apply the same conservative filter
        # to those strings, including nested lists and dictionaries.
        return _redact_sensitive_text(value)
    return value


_SENSITIVE_TEXT_ASSIGNMENT = re.compile(
    # Allow common prefixes/suffixes (e.g. DB_PASSWORD, client_secret,
    # AWS_SECRET_ACCESS_KEY, access_token) instead of requiring a standalone
    # word. Underscores are separators for credential names, not safe boundaries.
    r"(?i)(?<![A-Za-z0-9])"
    r"([A-Za-z0-9_-]*(?:password|secret|token|credential|api[_-]?key|"
    r"access[_-]?(?:key|token)|private[_-]?key|authorization|cookie)"
    r"[A-Za-z0-9_-]*)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_CREDENTIAL = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_URL_USERINFO_CREDENTIAL = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@:\s]+:[^/@\s]+@"
)


def _redact_sensitive_text(value):
    """Redact common credential assignments, bearer tokens, and URL userinfo."""
    if not isinstance(value, str):
        return value
    value = _URL_USERINFO_CREDENTIAL.sub(r"\1[REDACTED]@", value)
    value = _SENSITIVE_TEXT_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    return _BEARER_CREDENTIAL.sub(r"\1[REDACTED]", value)


class MetricComparisonCreate(BaseModel):
    tolerances: dict[str, float] = Field(default_factory=dict)

    @field_validator("tolerances")
    @classmethod
    def validate_tolerances(cls, value):
        import math

        normalized = {}
        for name, tolerance in value.items():
            if not name.strip():
                raise ValueError("Tolerance metric names must not be blank")
            if not math.isfinite(tolerance) or tolerance < 0:
                raise ValueError("Metric tolerances must be finite and non-negative")
            normalized[name.strip()] = tolerance
        return normalized
