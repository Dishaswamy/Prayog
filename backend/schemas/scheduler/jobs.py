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


class JobEdit(BaseModel):
    """Only the fields sent are changed. Same checks as a submission (FR-GPU-130)."""
    model_config = ConfigDict(str_strip_whitespace=True)

    gpus: Any = Field(default=None, description="Positive whole number of GPUs on one node")
    walltime: Any = Field(default=None, description="HH:MM", examples=["02:00"])
    snapshot_hash: str | None = Field(default=None, max_length=128, description="New snapshot after a config change")


class JobOut(BaseModel):
    id: int
    state: str
    queue_reason: str | None       # why it is still QUEUED (FR-GPU-021)
    p0_free_at: datetime | None    # QUEUED on busy own-lab units: guaranteed free by (FR-GPU-108)
    gpus: int
    walltime: str                  # HH:MM
    submitted_at: datetime
    node_id: int | None            # SCHEDULED or RUNNING: where (FR-GPU-022)
    node_name: str | None
    unit_indexes: list[int]
    # What the job is doing right now (PRAYOG-62)
    started_at: datetime | None            # confirmed start of the current or last run
    walltime_ends_at: datetime | None      # RUNNING: when walltime stops it
    stop_reason: str | None                # RUNNING: a stop the agent is carrying out
    planned_stop_at: datetime | None       # RUNNING: a maintenance window will stop it (FR-GPU-109)
    planned_stop_reason: str | None
    ended_at: datetime | None              # finished: how the last run ended
    end_state: str | None
    exit_code: int | None
    cancel_reason: str | None
    dispatch_attempts: int


class CancelOut(BaseModel):
    id: int
    state: str                     # CANCELLED, or RUNNING while the agent stops it
    stop_pending: bool
    message: str

    @classmethod
    def from_result(cls, r) -> "CancelOut":
        message = ("Stopping: the job becomes CANCELLED once its node confirms the stop." if r.stop_pending
                   else "Job cancelled.")
        return cls(id=r.job_id, state=r.state, stop_pending=r.stop_pending, message=message)


class MemberUsage(BaseModel):
    user_id: str                   # public ID, as on the account
    consumed_gpu_hours: float


class QuotaOut(BaseModel):
    """The sponsorship's GPU-hour cap and its use (FR-GPU-178)."""
    cap_gpu_hours: float
    consumed_gpu_hours: float
    committed_gpu_hours: float     # requested by QUEUED, SCHEDULED and RUNNING jobs
    remaining_gpu_hours: float
    members: list[MemberUsage]
