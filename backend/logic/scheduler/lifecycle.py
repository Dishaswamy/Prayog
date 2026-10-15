"""
After the claim: a placed job starting, being asked to stop, and its GPUs coming
back (GPU requirements sections 7.5 to 7.7; FR-GPU-057, 064, 068, 069, 097, 100,
126, 127, 135, 143, 180; NFR-GPU-018, 061).

    mark_running(job)          the agent confirmed the start: RUNNING, a run record,
                               and the GPU-time clock starts (FR-GPU-064, 100)
    request_stop(job, reason)  stop a placed job: a SCHEDULED job is released at once;
                               a RUNNING job is marked for the agent to stop, and its
                               units stay held until the agent confirms (FR-GPU-069)
    release(job, reason)       the allocation ends: units freed, the job terminal or back
                               in the queue, the run closed, GPU time recorded, in one
                               transaction

Dispatch, the agent's reports, cancel, walltime, maintenance, drains and node failure
call these; this module owns only the allocation, its run and its accounting. Each
locks the job row and waits for it (no SKIP LOCKED): unlike a pass, the caller must act
on this particular job. After a release commits, the caller triggers a pass so the
freed units are used.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

import models as orm
from logic.errors import ConflictError, NotFoundError
from logic.scheduler.claim import ClaimInvariantError, utc_now
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import JobState, ReleaseReason, RunEndState

# Platform-side stops: the job goes back to the queue with its original submit time.
REQUEUE = {ReleaseReason.NODE_FAILURE, ReleaseReason.DISPATCH_TIMEOUT,
           ReleaseReason.MAINTENANCE, ReleaseReason.EMERGENCY_DRAIN}
# Runs ending this way are charged to the consumer lab; INTERRUPTED runs are not (FR-GPU-143).
CHARGED = {ReleaseReason.COMPLETED, ReleaseReason.FAILED, ReleaseReason.WALLTIME_EXCEEDED, ReleaseReason.CANCELLED}
DISPATCH_FAILED = "DISPATCH_FAILED"

# Which job states each reason may end. A run must have started to complete, fail or
# hit its walltime; a dispatch can only time out before the agent acknowledged it.
PLACED = {JobState.SCHEDULED, JobState.RUNNING}
ALLOWED_FROM = {
    ReleaseReason.COMPLETED: {JobState.RUNNING},
    ReleaseReason.FAILED: {JobState.RUNNING},
    ReleaseReason.WALLTIME_EXCEEDED: {JobState.RUNNING},
    ReleaseReason.CANCELLED: PLACED,
    ReleaseReason.NODE_FAILURE: PLACED,
    ReleaseReason.DISPATCH_TIMEOUT: {JobState.SCHEDULED},
    ReleaseReason.MAINTENANCE: PLACED,
    ReleaseReason.EMERGENCY_DRAIN: PLACED,
}


@dataclass(frozen=True)
class ReleaseResult:
    job_id: int
    allocation_id: int
    unit_ids: tuple[int, ...]
    job_state: JobState               # terminal, or QUEUED for a requeue
    gpu_seconds: float                # 0 if the run never started
    charged: bool


@dataclass(frozen=True)
class StopResult:
    job_id: int
    released: bool                    # True: the job was not running yet and its units are free now
    stop_reason: ReleaseReason        # the reason the run will end with (the first stop requested wins)


def mark_running(session: Session, job_id: int, now: datetime | None = None) -> orm.Run:
    """
    The agent confirmed the start: SCHEDULED -> RUNNING, acknowledged_at = now, and a
    run record (FR-GPU-064). A job that is no longer SCHEDULED (cancelled or requeued
    meanwhile) is refused, so the agent must not start it (architecture section 7.5).
    """
    now = now or utc_now()
    try:
        job = _lock_job(session, job_id)
        if job.state != JobState.SCHEDULED:
            raise ConflictError(f"Job {job_id} is {job.state}, not SCHEDULED, so it cannot start.")
        allocation = _lock_live_allocation(session, job_id)
        allocation.acknowledged_at = now
        job.state = JobState.RUNNING
        run = orm.Run(job_id=job.id, allocation_id=allocation.id, node_id=allocation.node_id, started_at=now)
        session.add(run)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return run


def request_stop(
    session: Session,
    job_id: int,
    reason: ReleaseReason,
    now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> StopResult:
    """
    Stop a placed job. Not started yet (SCHEDULED): the claim is rolled back in this
    same transaction (FR-GPU-067). RUNNING: the allocation is marked for the agent to
    stop (SIGTERM, stop_grace, SIGKILL; FR-GPU-068); the units are released only when
    the agent confirms (FR-GPU-069). A second request keeps the first reason.
    """
    now = now or utc_now()
    reason = ReleaseReason(reason)
    try:
        job = _lock_job(session, job_id)
        if job.state not in PLACED:
            raise ConflictError(f"Job {job_id} is {job.state}; only a SCHEDULED or RUNNING job can be stopped.")
        if job.state == JobState.SCHEDULED:
            _release(session, job, reason, now, cfg)
            session.commit()
            return StopResult(job_id, released=True, stop_reason=reason)
        allocation = _lock_live_allocation(session, job_id)
        if allocation.stop_requested_at is None:
            allocation.stop_requested_at = now
            allocation.stop_reason = reason
        result = StopResult(job_id, released=False, stop_reason=ReleaseReason(allocation.stop_reason))
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return result


def release(
    session: Session,
    job_id: int,
    reason: ReleaseReason,
    now: datetime | None = None,
    exit_code: int | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> ReleaseResult:
    """
    End the job's live allocation. Terminal reasons move the job to the matching
    terminal state; platform stops return it to QUEUED with its original submit time
    and the reason recorded (FR-GPU-079, 085, 126, 135; NFR-GPU-016). A dispatch that
    times out max_dispatch_attempts times fails the job (FR-GPU-127). An allocation is
    never released twice (NFR-GPU-061): a second call is refused.
    """
    now = now or utc_now()
    try:
        job = _lock_job(session, job_id)
        result = _release(session, job, ReleaseReason(reason), now, cfg, exit_code)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return result


def _release(
    session: Session,
    job: orm.Job,
    reason: ReleaseReason,
    now: datetime,
    cfg: SchedulerConfig,
    exit_code: int | None = None,
) -> ReleaseResult:
    if job.state not in ALLOWED_FROM[reason]:
        raise ConflictError(f"Job {job.id} is {job.state}; {reason} cannot end it.")
    allocation = _lock_live_allocation(session, job.id)

    unit_ids = tuple(session.scalars(
        select(orm.AllocationUnit.unit_id)
        .where(orm.AllocationUnit.allocation_id == allocation.id, orm.AllocationUnit.released_at.is_(None))
        .order_by(orm.AllocationUnit.unit_id)
    ).all())
    allocation.released_at = now
    allocation.release_reason = reason
    session.execute(
        update(orm.AllocationUnit)
        .where(orm.AllocationUnit.allocation_id == allocation.id, orm.AllocationUnit.released_at.is_(None))
        .values(released_at=now)
        .execution_options(synchronize_session=False)
    )
    # The claim marker is cleared only where it still points at this allocation.
    freed = session.execute(
        update(orm.GpuUnit)
        .where(orm.GpuUnit.current_allocation_id == allocation.id)
        .values(current_allocation_id=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    if freed != len(unit_ids):  # pragma: no cover - the claim sets exactly these markers
        raise ClaimInvariantError(f"job {job.id}: freed {freed} of {len(unit_ids)} units")

    if reason == ReleaseReason.DISPATCH_TIMEOUT:
        job.dispatch_attempts += 1
    if reason == ReleaseReason.DISPATCH_TIMEOUT and job.dispatch_attempts >= cfg.max_dispatch_attempts:
        job.state = JobState.FAILED                   # FR-GPU-127, failure category PLATFORM
        job.state_reason = DISPATCH_FAILED
    elif reason in REQUEUE:
        job.state = JobState.QUEUED                   # submit time untouched: it keeps its place
        job.state_reason = reason                     # the next pass replaces it with a queue reason
    else:
        job.state = JobState(reason.value)
        job.state_reason = None

    run = session.scalars(select(orm.Run).where(orm.Run.allocation_id == allocation.id)).first()
    if run is not None:
        run.ended_at = now
        run.end_state = RunEndState.INTERRUPTED if reason in REQUEUE else RunEndState(reason.value)
        run.end_reason = reason
        run.exit_code = exit_code

    gpu_seconds, charged = _record_usage(session, job, allocation, len(unit_ids), reason, now)
    session.flush()
    return ReleaseResult(job.id, allocation.id, unit_ids, JobState(job.state), gpu_seconds, charged)


def _record_usage(
    session: Session, job: orm.Job, allocation: orm.Allocation, units: int, reason: ReleaseReason, now: datetime
) -> tuple[float, bool]:
    """
    Units x (confirmed end - confirmed start), FR-GPU-100. Time SCHEDULED before
    the start is not charged, so a run that never started records nothing. The
    consumer is the job's lab (its sponsorship group for course accounts, FR-GPU-120,
    180); the provider is the node's owner lab.
    """
    if allocation.acknowledged_at is None:
        return 0.0, False
    gpu_seconds = units * max(0.0, (now - allocation.acknowledged_at).total_seconds())
    charged = reason in CHARGED
    provider = session.scalar(select(orm.Node.owner_lab_id).where(orm.Node.id == allocation.node_id))
    session.add(orm.UsageEntry(
        job_id=job.id,
        consumer_lab_id=job.lab_id,
        provider_lab_id=provider,
        gpu_seconds=gpu_seconds,
        ended_at=now,
        charged=charged,
    ))
    return gpu_seconds, charged


def _lock_job(session: Session, job_id: int) -> orm.Job:
    job = session.scalars(
        select(orm.Job).where(orm.Job.id == job_id).with_for_update().execution_options(populate_existing=True)
    ).first()
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    return job


def _lock_live_allocation(session: Session, job_id: int) -> orm.Allocation:
    allocation = session.scalars(
        select(orm.Allocation)
        .where(orm.Allocation.job_id == job_id, orm.Allocation.released_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if allocation is None:
        raise ConflictError(f"Job {job_id} holds no live allocation.")
    return allocation
