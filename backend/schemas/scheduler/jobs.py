from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobSubmit(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # gpus and walltime are checked by the scheduler, not here, so that every bad
    # value is answered with a rejection code (FR-GPU-016) rather than a generic 422.
    gpus: Any = Field(description="Positive whole number of GPUs on one node")
    walltime: Any = Field(description="HH:MM", examples=["04:00"])
    client_request_id: str = Field(min_length=1, max_length=64, description="Same ID on a retry returns the same job")
    project_ref: str | None = Field(default=None, max_length=255)
    snapshot_hash: str | None = Field(default=None, max_length=128)


class SubmissionOut(BaseModel):
    request_id: int
    status: str                    # IN_PROCESS for a new request; the current state for a retry
    message: str


class JobOut(BaseModel):
    id: int
    state: str
    queue_reason: str | None       # why it is still QUEUED (FR-GPU-021)
    gpus: int
    walltime: str                  # HH:MM
    submitted_at: datetime
    node_id: int | None            # SCHEDULED or RUNNING: where (FR-GPU-022)
    node_name: str | None
    unit_indexes: list[int]
