from datetime import datetime

from pydantic import BaseModel, Field

from logic.scheduler.dispatch import Outcome


class EnrolIn(BaseModel):
    enrolment_token: str = Field(min_length=1, max_length=200)


class EnrolOut(BaseModel):
    node_id: int
    credential: str                  # send as X-Node-Credential on every later call; shown once


class HeartbeatIn(BaseModel):
    unit_count: int = Field(ge=0, description="GPU units the agent sees")
    running_allocation_ids: list[int] = Field(default_factory=list, description="Allocations with a live container")


class HeartbeatOut(BaseModel):
    state: str
    terminate_allocation_ids: list[int]   # containers to stop and clean up: their allocation is gone (FR-GPU-136)


class AllocationOut(BaseModel):
    allocation_id: int                   # the idempotency key (FR-GPU-060)
    job_id: int
    unit_indexes: list[int]              # start only on these units (FR-GPU-063)
    walltime_minutes: int
    snapshot_hash: str | None
    started_at: datetime | None
    walltime_ends_at: datetime | None    # enforce locally (FR-GPU-134)
    stop_reason: str | None              # set: stop the run, then report STOPPED


class AckOut(BaseModel):
    started: bool                        # False: already acknowledged earlier; do not start a second time


class ReportIn(BaseModel):
    seq: int = Field(ge=1, description="Increases with every report from this node (FR-GPU-128)")
    outcome: Outcome
    exit_code: int | None = None
    # The end of the run's stdout/stderr, for the Steward's diagnosis (FR-GPU-146). Optional;
    # the server keeps at most the last 64 KB and never serves it back.
    log_tail: str | None = Field(default=None, max_length=1_000_000)


class ReportOut(BaseModel):
    applied: bool                        # False: a repeat of a report already applied
    job_state: str | None
