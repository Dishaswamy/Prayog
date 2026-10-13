"""
Submitting a GPU request and reading its status (GPU requirements section 5,
FR-GPU-010 to 017, FR-GPU-105 to 107, FR-GPU-019 to 022).

A request is answered on the spot (FR-GPU-017): either accepted with a request
ID and IN PROCESS, or rejected with a code and a message naming the rule and
its limit (FR-GPU-016). A rejection writes nothing (FR-GPU-015). An accepted
request is stored QUEUED in the same transaction, so it is QUEUED well within
T_inprocess, and the caller then triggers a pass.

The acting lab and account type come from the server (scheduling_contexts),
never from the client.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import AppError, ConflictError, NotFoundError
from logic.scheduler.claim import utc_now
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import AccountType, DonationMode, JobState, NodeState

ACTIVE_STATES = (JobState.IN_PROCESS, JobState.QUEUED, JobState.SCHEDULED, JobState.RUNNING)
WALLTIME = re.compile(r"(\d{1,3}):([0-5]\d)")


class RejectCode(StrEnum):
    """FR-GPU-016. PROJECT_NOT_FOUND_OR_FORBIDDEN and QUOTA_EXCEEDED arrive with projects and course caps."""
    INVALID_GPUS = "INVALID_GPUS"
    INVALID_WALLTIME = "INVALID_WALLTIME"
    GPUS_EXCEED_MAX_NODE = "GPUS_EXCEED_MAX_NODE"
    WALLTIME_EXCEEDS_MAX = "WALLTIME_EXCEEDS_MAX"
    QUEUE_LIMIT_REACHED = "QUEUE_LIMIT_REACHED"
    AUTHORIZATION_INACTIVE = "AUTHORIZATION_INACTIVE"


class SubmissionRejected(AppError):
    """A REJECTED request. main.py returns its code beside the message."""

    def __init__(self, code: RejectCode, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = 403 if code == RejectCode.AUTHORIZATION_INACTIVE else 400


@dataclass(frozen=True)
class Submission:
    job: orm.Job
    created: bool                 # False: a retry, answered with the original job (FR-GPU-107)


# ── field rules ────────────────────────────────────────────────────────

def parse_gpus(value: Any) -> int:
    """A positive whole number (FR-GPU-010). JSON true/false are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SubmissionRejected(RejectCode.INVALID_GPUS, f"GPU count must be a positive whole number, got {value!r}.")
    return value


def parse_walltime(value: Any, cfg: SchedulerConfig = DEFAULT_CONFIG) -> int:
    """HH:MM, at least min_walltime (FR-GPU-011). Returns minutes."""
    match = WALLTIME.fullmatch(value) if isinstance(value, str) else None
    minutes = int(match[1]) * 60 + int(match[2]) if match else 0
    if minutes < cfg.min_walltime_minutes:
        raise SubmissionRejected(
            RejectCode.INVALID_WALLTIME,
            f"Walltime must be HH:MM and at least {format_walltime(cfg.min_walltime_minutes)}, got {value!r}.",
        )
    return minutes


def format_walltime(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ── submit ─────────────────────────────────────────────────────────────

def submit_job(
    db: Session,
    user_id: int,
    gpus: Any,
    walltime: Any,
    client_request_id: str,
    project_ref: str | None = None,
    snapshot_hash: str | None = None,
    now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> Submission:
    now = now or utc_now()                                        # server time (FR-GPU-012)

    # Locking the user's context row serialises their submissions, so neither the
    # queue limit nor the idempotency check can be beaten by two requests at once.
    context = db.scalars(
        select(orm.SchedulingContext).where(orm.SchedulingContext.user_id == user_id).with_for_update()
    ).first()
    if context is None:
        raise SubmissionRejected(RejectCode.AUTHORIZATION_INACTIVE, "You have no active GPU authorization.")

    original = db.scalars(
        select(orm.Job).where(orm.Job.user_id == user_id, orm.Job.client_request_id == client_request_id)
    ).first()
    if original is not None:
        if original.submitted_at >= now - timedelta(hours=cfg.idempotency_window_hours):
            db.commit()
            return Submission(original, created=False)
        raise ConflictError("This client request ID was already used for an earlier request.")

    gpu_count = parse_gpus(gpus)
    minutes = parse_walltime(walltime, cfg)
    if minutes > cfg.max_walltime_minutes:                        # FR-GPU-014
        raise SubmissionRejected(
            RejectCode.WALLTIME_EXCEEDS_MAX,
            f"Walltime {format_walltime(minutes)} exceeds the maximum of {format_walltime(cfg.max_walltime_minutes)}.",
        )

    largest = largest_reachable_node(db, context)                 # FR-GPU-013
    if gpu_count > largest:
        raise SubmissionRejected(
            RejectCode.GPUS_EXCEED_MAX_NODE,
            f"Requested {gpu_count} GPUs, but the largest node you can use has {largest}.",
        )

    active = db.scalar(
        select(func.count()).select_from(orm.Job)
        .where(orm.Job.user_id == user_id, orm.Job.state.in_(ACTIVE_STATES))
    )
    if active + 1 > cfg.max_active_jobs_per_user:                 # FR-GPU-105
        raise SubmissionRejected(
            RejectCode.QUEUE_LIMIT_REACHED,
            f"You already have {active} active jobs; the limit is {cfg.max_active_jobs_per_user}.",
        )

    job = orm.Job(
        user_id=user_id,
        project_ref=project_ref,
        snapshot_hash=snapshot_hash,
        lab_id=context.lab_id,
        account_type=context.account_type,
        gpus=gpu_count,
        walltime_minutes=minutes,
        state=JobState.QUEUED,
        client_request_id=client_request_id,
        submitted_at=now,
        effective_submit_at=now,
    )
    db.add(job)
    db.commit()
    return Submission(job, created=True)


def largest_reachable_node(db: Session, context: orm.SchedulingContext) -> int:
    """
    Unit count of the largest node this account could ever be eligible for,
    ignoring occupancy, maintenance and temporary node states: teaching-pool nodes
    for course accounts; shared and own-lab nodes outside it for research.
    """
    course = context.account_type == AccountType.COURSE
    return db.scalar(
        select(func.max(orm.Node.unit_count)).where(
            orm.Node.state != NodeState.DECOMMISSIONED,
            orm.Node.teaching_pool.is_(course),
            or_(orm.Node.donation_mode == DonationMode.SHARED, orm.Node.owner_lab_id == context.lab_id),
        )
    ) or 0


# ── status ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class JobStatus:
    job: orm.Job
    queue_reason: str | None              # QUEUED only (FR-GPU-021)
    node_id: int | None = None            # SCHEDULED or RUNNING (FR-GPU-022)
    node_name: str | None = None
    unit_indexes: tuple[int, ...] = ()


def job_status(db: Session, user_id: int, job_id: int) -> JobStatus:
    """The owner's view of one job. Someone else's job is reported as not found (NFR-GPU-030)."""
    job = db.get(orm.Job, job_id)
    if job is None or job.user_id != user_id:
        raise NotFoundError("Job not found.")

    queue_reason = job.state_reason if job.state == JobState.QUEUED else None
    row = db.execute(
        select(orm.Allocation.id, orm.Node.id, orm.Node.name)
        .join(orm.Node, orm.Node.id == orm.Allocation.node_id)
        .where(orm.Allocation.job_id == job.id, orm.Allocation.released_at.is_(None))
    ).first()
    if row is None:
        return JobStatus(job, queue_reason)

    allocation_id, node_id, node_name = row
    unit_indexes = db.scalars(
        select(orm.GpuUnit.unit_index)
        .join(orm.AllocationUnit, orm.AllocationUnit.unit_id == orm.GpuUnit.id)
        .where(orm.AllocationUnit.allocation_id == allocation_id)
        .order_by(orm.GpuUnit.unit_index)
    ).all()
    return JobStatus(job, queue_reason, node_id, node_name, tuple(unit_indexes))
