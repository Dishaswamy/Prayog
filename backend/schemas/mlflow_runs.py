from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from logic.project_versions import VersionInfo


class VersionRegister(BaseModel):
    manifest_artifact_id: str = Field(min_length=36, max_length=36)


class CurrentVersionSet(BaseModel):
    version: str = Field(min_length=1, max_length=8, description="A version label such as A or B")


class VersionOut(BaseModel):
    version: str                            # the label: A, B, ...
    version_number: int
    manifest_hash: str                      # BLAKE3 of the manifest: the version's identity
    manifest_artifact_id: str
    experiment_id: int                      # where the version's files are stored
    file_count: int
    created_at: datetime
    is_head: bool                           # the newest version
    is_current: bool                        # the version runs use by default
    run_count: int

    @classmethod
    def of(cls, info: VersionInfo) -> "VersionOut":
        v = info.version
        return cls(version=info.label, version_number=v.version_number, manifest_hash=v.manifest_hash,
                   manifest_artifact_id=v.manifest_artifact_id, experiment_id=v.experiment_id, file_count=v.file_count, created_at=v.created_at,
                   is_head=info.is_head, is_current=info.is_current, run_count=info.run_count)


class VersionRegisterOut(VersionOut):
    created: bool                           # False: this exact content was already a version


class StagingFile(BaseModel):
    blake3: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class StagingCheck(BaseModel):
    files: list[StagingFile] = Field(max_length=20000)


class StagingMissing(BaseModel):
    missing: list[str]                      # BLAKE3 digests that still have to be uploaded


class StagedOut(BaseModel):
    blake3: str
    size_bytes: int
    created: bool


class DatasetFileIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024, description="Relative path, as the node will see it under /data")
    size_bytes: int = Field(ge=0)
    blake3: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


class RunSubmit(BaseModel):
    version: str | None = Field(default=None, max_length=8, description="A version label; default: the current one")
    gpus: Any = Field(description="Positive whole number of GPUs on one node")
    walltime: Any = Field(description="HH:MM", examples=["04:00"])
    client_request_id: str = Field(min_length=1, max_length=64, description="Same ID on a retry returns the same run")
    entrypoint: str | None = Field(default=None, min_length=1, max_length=255, description="Default: main.py")
    hyperparameters: dict[str, Any] | None = None
    dataset: list[DatasetFileIn] = Field(default_factory=list, max_length=20000)
    rebuild_of: str | None = Field(default=None, max_length=16, description="A finished run to reproduce, such as A/1: "
                                   "same version, hyperparameters and dataset, checked against the recorded fingerprint")


class DatasetFileOut(BaseModel):
    path: str
    size_bytes: int
    blake3: str
    staged: bool                            # False once the temporary copy has been deleted


class CheckpointOut(BaseModel):
    step: int
    is_best: bool
    artifact_id: str
    size_bytes: int


class GpuRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: int
    label: str                              # "A run 1"
    version: str
    run_number: int
    job_id: int
    rebuild_of: str | None = None           # "A run 1" when this run reproduces another
    status: str                             # the run: QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED
    job_state: str                          # the scheduler's view while it waits or runs
    queue_reason: str | None
    gpus: int
    walltime: str
    hyperparameters: dict[str, Any]
    dataset: list[DatasetFileOut]
    checkpoints: list[CheckpointOut]
    metrics: dict[str, float]               # the last value of each metric
    logs_artifact_id: str | None
    snapshot_id: str | None
    error_summary: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created: bool = True                    # False: a retry that returned the original run


class ProjectStatusOut(BaseModel):
    project_id: str
    name: str
    current_version: str | None
    head_version: str | None
    versions: int
    runs: int
    latest_run: GpuRunOut | None
    staged_files: int                       # files still staged for this project's runs
    staged_bytes: int


class StagingClearOut(BaseModel):
    deleted_files: int
    kept_in_use: int
    freed_bytes: int


class MetricComparison(BaseModel):
    name: str
    step: int
    original: float
    rebuilt: float | None                   # None: the rebuild never logged it
    difference: float | None
    within_tolerance: bool


class WeightsComparison(BaseModel):
    original_blake3: str | None
    rebuilt_blake3: str | None
    identical: bool                         # the same bytes: a bit-exact reproduction


class RebuildComparison(BaseModel):
    rebuilt: str                            # "A run 3"
    original: str                           # "A run 1"
    rebuilt_status: str
    tolerance: float
    metrics: list[MetricComparison]
    metrics_match: bool                     # every original metric reproduced within the tolerance
    weights: WeightsComparison
    reproduced: bool                        # the rebuild succeeded and its metrics match
