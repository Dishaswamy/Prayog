"""
After the claim: a placed job starting, and its GPUs coming back (GPU requirements
section 7.5; FR-GPU-057, 097, 100, 126, 135, 143, 180; NFR-GPU-018, 061).

    mark_running(job)        the agent acknowledged the dispatch: RUNNING, and the
                             GPU-time clock starts (confirmed start, FR-GPU-100)
    release(job, reason)     the allocation ends: units freed, the job terminal or
                             back in the queue, GPU time recorded, one transaction

Dispatch, the agent's reports, cancel, walltime and node failure call these; this
module owns only the allocation and its accounting. Both lock the job row and wait
for it (no SKIP LOCKED): unlike a pass, the caller must act on this particular job.
After a release commits, the caller triggers a pass (Trigger.RUN_END) so the freed
units are used.

Release hooks (register_release_hook) run inside the release transaction, each in its
own savepoint, so a subsystem that records the end of a run (the Steward, SADD §8.3)
commits atomically with it. A failing hook is rolled back to its savepoint and logged;
the release itself always commits (NFR-GPU-055: scheduling never depends on the Steward).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

import models as orm
from logic.errors import ConflictError, NotFoundError
from logic.scheduler.claim import ClaimInvariantError, utc_now
from logic.scheduler.domain import JobState, ReleaseReason

log = logging.getLogger("prayog.scheduler")

REQUEUE = {ReleaseReason.NODE_FAILURE, ReleaseReason.DISPATCH_TIMEOUT}
# Runs ending this way are charged to the consumer lab; INTERRUPTED (node failure) is not (FR-GPU-143).
CHARGED = {ReleaseReason.COMPLETED, ReleaseReason.FAILED, ReleaseReason.WALLTIME_EXCEEDED, ReleaseReason.CANCELLED}

# Which job states each reason may end. A run must have started to complete, fail or
# hit its walltime; a dispatch can only time out before the agent acknowledged it.
ALLOWED_FROM = {
    ReleaseReason.COMPLETED: {JobState.RUNNING},
    ReleaseReason.FAILED: {JobState.RUNNING},
    ReleaseReason.WALLTIME_EXCEEDED: {JobState.RUNNING},
    ReleaseReason.CANCELLED: {JobState.SCHEDULED, JobState.RUNNING},
    ReleaseReason.NODE_FAILURE: {JobState.SCHEDULED, JobState.RUNNING},
    ReleaseReason.DISPATCH_TIMEOUT: {JobState.SCHEDULED},
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
class RunReport:
    """What the node agent reports when a run ends (FR-GPU-146). Optional: cancel,
    walltime and node failure release without one."""
    exit_code: int | None = None
    failure_category: str | None = None
    log_tail: str | None = None          # the end of stdout/stderr; kept at most LOG_TAIL_LIMIT


LOG_TAIL_LIMIT = 64 * 1024

ReleaseHook = Callable[[Session, ReleaseResult, ReleaseReason, datetime], None]
RELEASE_HOOKS: list[ReleaseHook] = []


def register_release_hook(hook: ReleaseHook) -> None:
    """Run hook(session, result, reason, now) inside every release transaction."""
    if hook not in RELEASE_HOOKS:
        RELEASE_HOOKS.append(hook)


def mark_running(session: Session, job_id: int, now: datetime | None = None) -> orm.Allocation:
    """The agent confirmed the start: SCHEDULED -> RUNNING, acknowledged_at = now."""
    now = now or utc_now()
    try:
        job = _lock_job(session, job_id)
        if job.state != JobState.SCHEDULED:
            raise ConflictError(f"Job {job_id} is {job.state}, not SCHEDULED, so it cannot start.")
        allocation = _lock_live_allocation(session, job_id)
        allocation.acknowledged_at = now
        job.state = JobState.RUNNING
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return allocation


def release(
    session: Session,
    job_id: int,
    reason: ReleaseReason,
    now: datetime | None = None,
    report: RunReport | None = None,
) -> ReleaseResult:
    """
    End the job's live allocation. Terminal reasons move the job to the matching
    terminal state; NODE_FAILURE and DISPATCH_TIMEOUT return it to QUEUED with its
    original submit time and the reason recorded (FR-GPU-126, 135; NFR-GPU-016).
    An allocation is never released twice (NFR-GPU-061): a second call is refused.
    The agent's report, if any, is stored with the release (run_results), and the
    release hooks run before the commit.
    """
    now = now or utc_now()
    reason = ReleaseReason(reason)
    try:
        result = _release(session, job_id, reason, now)
        if report is not None:
            session.add(orm.RunResult(
                allocation_id=result.allocation_id,
                job_id=job_id,
                exit_code=report.exit_code,
                failure_category=report.failure_category,
                log_tail=report.log_tail[-LOG_TAIL_LIMIT:] if report.log_tail else None,
            ))
            session.flush()
        _run_release_hooks(session, result, reason, now)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return result


def _run_release_hooks(session: Session, result: ReleaseResult, reason: ReleaseReason, now: datetime) -> None:
    for hook in RELEASE_HOOKS:
        try:
            with session.begin_nested():          # a savepoint: a failing hook undoes only itself
                hook(session, result, reason, now)
        except Exception:
            log.exception("release hook %s failed for job %s; the release still commits",
                          getattr(hook, "__name__", hook), result.job_id)


def _release(session: Session, job_id: int, reason: ReleaseReason, now: datetime) -> ReleaseResult:
    job = _lock_job(session, job_id)
    if job.state not in ALLOWED_FROM[reason]:
        raise ConflictError(f"Job {job_id} is {job.state}; {reason} cannot end it.")
    allocation = _lock_live_allocation(session, job_id)

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
        raise ClaimInvariantError(f"job {job_id}: freed {freed} of {len(unit_ids)} units")

    if reason in REQUEUE:
        job.state = JobState.QUEUED
        job.state_reason = reason                 # the next pass replaces it with a queue reason
    else:
        job.state = JobState(reason.value)
        job.state_reason = None

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
