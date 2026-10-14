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
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import AppError, ConflictError, NotFoundError
from logic.scheduler.claim import utc_now
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import AccountType, DonationMode, JobState, NodeState
from logic.scheduler.loading import load_nodes, to_job_view
from logic.scheduler.waiting import p0_guaranteed_free_at
from models import User

ACTIVE_STATES = (JobState.IN_PROCESS, JobState.QUEUED, JobState.SCHEDULED, JobState.RUNNING)
WALLTIME = re.compile(r"(\d{1,3}):([0-5]\d)")


class RejectCode(StrEnum):
    """FR-GPU-016 and FR-GPU-131. PROJECT_NOT_FOUND_OR_FORBIDDEN arrives with projects."""
    INVALID_GPUS = "INVALID_GPUS"
    INVALID_WALLTIME = "INVALID_WALLTIME"
    GPUS_EXCEED_MAX_NODE = "GPUS_EXCEED_MAX_NODE"
    WALLTIME_EXCEEDS_MAX = "WALLTIME_EXCEEDS_MAX"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    QUEUE_LIMIT_REACHED = "QUEUE_LIMIT_REACHED"
    AUTHORIZATION_INACTIVE = "AUTHORIZATION_INACTIVE"
    JOB_NOT_QUEUED = "JOB_NOT_QUEUED"


_STATUS = {RejectCode.AUTHORIZATION_INACTIVE: 403, RejectCode.JOB_NOT_QUEUED: 409}


class SubmissionRejected(AppError):
    """A REJECTED request or edit. main.py returns its code beside the message."""

    def __init__(self, code: RejectCode, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = _STATUS.get(code, 400)


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
    check_request(db, context.lab_id, context.account_type, gpu_count, minutes, cfg, cap=context.course_cap_gpu_hours)

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


def check_request(
    db: Session,
    lab_id: int,
    account_type: str,
    gpu_count: int,
    minutes: int,
    cfg: SchedulerConfig,
    cap: Decimal | None,
    editing_job_id: int | None = None,
) -> None:
    """
    The rules a request must pass beyond its own fields, shared by submit and edit
    (FR-GPU-130): walltime ceiling, largest reachable node, and the course cap.
    """
    if minutes > cfg.max_walltime_minutes:                        # FR-GPU-014
        raise SubmissionRejected(
            RejectCode.WALLTIME_EXCEEDS_MAX,
            f"Walltime {format_walltime(minutes)} exceeds the maximum of {format_walltime(cfg.max_walltime_minutes)}.",
        )

    largest = largest_reachable_node(db, lab_id, account_type)    # FR-GPU-013
    if gpu_count > largest:
        raise SubmissionRejected(
            RejectCode.GPUS_EXCEED_MAX_NODE,
            f"Requested {gpu_count} GPUs, but the largest node you can use has {largest}.",
        )

    if account_type == AccountType.COURSE and cap is not None:    # FR-GPU-104
        # Locking the sponsorship's row serialises its members' submissions and edits,
        # so two members cannot both spend the last of the cap.
        db.execute(select(orm.Lab.id).where(orm.Lab.id == lab_id).with_for_update())
        quota = sponsorship_usage(db, lab_id, cap, exclude_job_id=editing_job_id)
        requested = gpu_hours(gpu_count, minutes)
        if requested > quota.remaining:
            raise SubmissionRejected(
                RejectCode.QUOTA_EXCEEDED,
                f"Requested {requested:g} GPU-hours, but your sponsorship has {quota.remaining:g} of its "
                f"{quota.cap:g} GPU-hour cap left.",
            )


def gpu_hours(gpu_count: int, minutes: int) -> float:
    return round(gpu_count * minutes / 60, 4)


def largest_reachable_node(db: Session, lab_id: int, account_type: str) -> int:
    """
    Unit count of the largest node this account could ever be eligible for,
    ignoring occupancy, maintenance and temporary node states: teaching-pool nodes
    for course accounts; shared and own-lab nodes outside it for research.
    """
    course = account_type == AccountType.COURSE
    return db.scalar(
        select(func.max(orm.Node.unit_count)).where(
            orm.Node.state != NodeState.DECOMMISSIONED,
            orm.Node.teaching_pool.is_(course),
            or_(orm.Node.donation_mode == DonationMode.SHARED, orm.Node.owner_lab_id == lab_id),
        )
    ) or 0


# ── course cap (FR-GPU-104, FR-GPU-178) ────────────────────────────────

@dataclass(frozen=True)
class SponsorshipUsage:
    cap: float
    consumed: float                       # charged GPU-hours of finished runs
    committed: float                      # requested GPU-hours of QUEUED, SCHEDULED and RUNNING jobs
    members: tuple[tuple[str, float], ...] = ()   # (public user ID, consumed GPU-hours)

    @property
    def remaining(self) -> float:
        return round(max(0.0, self.cap - self.consumed - self.committed), 4)


def sponsorship_usage(
    db: Session, lab_id: int, cap: Decimal | float, exclude_job_id: int | None = None
) -> SponsorshipUsage:
    """
    remaining = cap - consumed - requested GPU-hours of the sponsorship's QUEUED,
    SCHEDULED and RUNNING jobs (FR-GPU-104). An edited job's own request is left out,
    since the edit replaces it.
    """
    consumed_by_member = db.execute(
        select(User.user_id, func.coalesce(func.sum(orm.UsageEntry.gpu_seconds), 0.0))
        .join(orm.Job, orm.Job.id == orm.UsageEntry.job_id)
        .join(User, User.id == orm.Job.user_id)
        .where(orm.UsageEntry.consumer_lab_id == lab_id, orm.UsageEntry.charged.is_(True))
        .group_by(User.user_id)
        .order_by(User.user_id)
    ).all()
    members = tuple((public_id, round(seconds / 3600, 4)) for public_id, seconds in consumed_by_member)

    open_jobs = select(orm.Job.gpus, orm.Job.walltime_minutes).where(
        orm.Job.lab_id == lab_id, orm.Job.state.in_((JobState.QUEUED, JobState.SCHEDULED, JobState.RUNNING))
    )
    if exclude_job_id is not None:
        open_jobs = open_jobs.where(orm.Job.id != exclude_job_id)
    committed = sum(gpu_hours(g, m) for g, m in db.execute(open_jobs))

    return SponsorshipUsage(
        cap=float(cap),
        consumed=round(sum(hours for _, hours in members), 4),
        committed=round(committed, 4),
        members=members,
    )


def quota_status(db: Session, user_id: int) -> SponsorshipUsage:
    """The requester's sponsorship cap and its use (FR-GPU-178). Course accounts with a cap only."""
    context = db.get(orm.SchedulingContext, user_id)
    if context is None or context.account_type != AccountType.COURSE or context.course_cap_gpu_hours is None:
        raise NotFoundError("No GPU-hour cap applies to your account.")
    return sponsorship_usage(db, context.lab_id, context.course_cap_gpu_hours)


# ── edit (FR-GPU-070 to 072, FR-GPU-130 to 132) ────────────────────────

UNCHANGED: Any = object()


def edit_job(
    db: Session,
    user_id: int,
    job_id: int,
    gpus: Any = UNCHANGED,
    walltime: Any = UNCHANGED,
    snapshot_hash: Any = UNCHANGED,
    now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> orm.Job:
    """
    Change a QUEUED job's GPU count, walltime or snapshot. Validated with the
    submission rules (FR-GPU-130); a rejected edit leaves the job unchanged. Growing
    either GPUs or walltime resets the age clock to now (FR-GPU-072), otherwise it is
    kept (FR-GPU-071), so editing cannot be used to farm queue age.

    The job row is locked for the edit (FR-GPU-131). A claim on the same job skips a
    locked row, and an edit that waits for a claim then finds it SCHEDULED: the two
    can never both succeed (A-67).
    """
    now = now or utc_now()
    try:
        job = db.scalars(
            select(orm.Job).where(orm.Job.id == job_id).with_for_update().execution_options(populate_existing=True)
        ).first()
        if job is None or job.user_id != user_id:
            raise NotFoundError("Job not found.")
        if job.state != JobState.QUEUED:
            raise SubmissionRejected(
                RejectCode.JOB_NOT_QUEUED, f"Job {job_id} is {job.state}; only QUEUED jobs can be edited."
            )

        new_gpus = job.gpus if gpus is UNCHANGED else parse_gpus(gpus)
        new_minutes = job.walltime_minutes if walltime is UNCHANGED else parse_walltime(walltime, cfg)
        context = db.get(orm.SchedulingContext, user_id)
        cap = context.course_cap_gpu_hours if context is not None else None
        check_request(db, job.lab_id, job.account_type, new_gpus, new_minutes, cfg, cap=cap, editing_job_id=job.id)

        if new_gpus > job.gpus or new_minutes > job.walltime_minutes:
            job.effective_submit_at = now                         # grew: back of the queue (FR-GPU-072)
        job.gpus = new_gpus
        job.walltime_minutes = new_minutes
        if snapshot_hash is not UNCHANGED:
            job.snapshot_hash = snapshot_hash                     # a configuration edit (FR-GPU-132)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return job


# ── status ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class JobStatus:
    job: orm.Job
    queue_reason: str | None              # QUEUED only (FR-GPU-021)
    node_id: int | None = None            # SCHEDULED or RUNNING (FR-GPU-022)
    node_name: str | None = None
    unit_indexes: tuple[int, ...] = ()
    p0_free_at: datetime | None = None    # QUEUED on busy own-lab units: guaranteed free by (FR-GPU-108)


def job_status(
    db: Session, user_id: int, job_id: int, now: datetime | None = None, cfg: SchedulerConfig = DEFAULT_CONFIG
) -> JobStatus:
    """The owner's view of one job. Someone else's job is reported as not found (NFR-GPU-030)."""
    now = now or utc_now()
    job = db.get(orm.Job, job_id)
    if job is None or job.user_id != user_id:
        raise NotFoundError("Job not found.")

    if job.state == JobState.QUEUED:
        return JobStatus(job, job.state_reason, p0_free_at=_p0_free_at(db, job, now, cfg))

    row = db.execute(
        select(orm.Allocation.id, orm.Node.id, orm.Node.name)
        .join(orm.Node, orm.Node.id == orm.Allocation.node_id)
        .where(orm.Allocation.job_id == job.id, orm.Allocation.released_at.is_(None))
    ).first()
    if row is None:
        return JobStatus(job, None)

    allocation_id, node_id, node_name = row
    unit_indexes = db.scalars(
        select(orm.GpuUnit.unit_index)
        .join(orm.AllocationUnit, orm.AllocationUnit.unit_id == orm.GpuUnit.id)
        .where(orm.AllocationUnit.allocation_id == allocation_id)
        .order_by(orm.GpuUnit.unit_index)
    ).all()
    return JobStatus(job, None, node_id, node_name, tuple(unit_indexes))


def _p0_free_at(db: Session, job: orm.Job, now: datetime, cfg: SchedulerConfig) -> datetime | None:
    own_nodes = db.scalars(select(orm.Node.id).where(orm.Node.owner_lab_id == job.lab_id)).all()
    if not own_nodes:
        return None
    nodes = load_nodes(db, now, own_nodes)
    # A busy unit is guaranteed free once its run is past start + walltime + the completion
    # bound; a run not yet started is assumed to start now.
    rows = db.execute(
        select(orm.GpuUnit.id, orm.Allocation.acknowledged_at, orm.Job.walltime_minutes)
        .join(orm.Allocation, orm.Allocation.id == orm.GpuUnit.current_allocation_id)
        .join(orm.Job, orm.Job.id == orm.Allocation.job_id)
        .where(orm.GpuUnit.node_id.in_(own_nodes))
    )
    bound = timedelta(seconds=cfg.walltime_completion_bound_seconds)
    busy_until = {unit_id: (started or now) + timedelta(minutes=walltime) + bound for unit_id, started, walltime in rows}
    return p0_guaranteed_free_at(to_job_view(job), nodes, busy_until, now)
