"""
Cancelling a job (GPU requirements section 7.6; FR-GPU-065 to 069, UC-GPU-09).

    QUEUED      -> CANCELLED at once; no units to release (FR-GPU-066)
    SCHEDULED   -> CANCELLED and the claim rolled back in the same transaction (FR-GPU-067)
    RUNNING     -> the agent is asked to stop it (SIGTERM, stop_grace, SIGKILL; FR-GPU-068);
                   it becomes CANCELLED when the agent confirms, and only then are the
                   units released (FR-GPU-069)

The owner may cancel their own job; anyone else is refused with insufficient permission,
and the attempt is logged (FR-GPU-065). A Platform Admin may cancel any user's job, but
only with a reason; the reason, the admin and the time go to the audit log in the same
transaction as the cancellation (FR-GPU-065, 099; US-ADM-07).
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from logic.scheduler.claim import utc_now
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import JobState, ReleaseReason
from logic.scheduler.lifecycle import request_stop

audit = logging.getLogger("prayog.audit")


@dataclass(frozen=True)
class CancelResult:
    job_id: int
    state: JobState               # CANCELLED, or RUNNING while the agent stops it
    stop_pending: bool


def cancel_job(
    db: Session,
    job_id: int,
    user_id: int,
    now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> CancelResult:
    now = now or utc_now()
    try:
        job = _lock_job(db, job_id)
        if job.user_id != user_id:
            audit.warning("refused: user %s tried to cancel job %s owned by user %s", user_id, job_id, job.user_id)
            raise ForbiddenError("You do not have permission to cancel this job.")
        job.cancel_reason = "owner"
        result = _cancel_locked(db, job, now, cfg)
    except BaseException:
        db.rollback()
        raise
    audit.info("job %s cancelled by its owner, user %s", job_id, user_id)
    return result


def admin_cancel_job(
    db: Session,
    job_id: int,
    admin_id: int,
    reason: str | None,
    now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> CancelResult:
    """
    A Platform Admin cancels any user's job (US-ADM-07). The reason is mandatory; it is
    kept on the job and written, with the admin and the time, to the audit log in the
    cancellation's own transaction (FR-GPU-065, 099).
    """
    now = now or utc_now()
    if not reason or not reason.strip():
        raise BadRequestError("A reason is required to cancel another user's job.")
    reason = reason.strip()
    try:
        job = _lock_job(db, job_id)
        owner_id = job.user_id
        job.cancel_reason = f"admin: {reason}"[:255]
        db.add(orm.AuditEvent(
            at=now, actor=str(admin_id), actor_kind="user", operation="job.cancel_by_admin",
            resource_ids=[f"job:{job_id}", f"owner:{owner_id}"], outcome="CANCEL_REQUESTED",
            reason=reason[:200],
        ))
        result = _cancel_locked(db, job, now, cfg)
    except BaseException:
        db.rollback()
        raise
    audit.info("job %s of user %s cancelled by admin %s: %s", job_id, owner_id, admin_id, reason)
    return result


def _lock_job(db: Session, job_id: int) -> orm.Job:
    job = db.scalars(
        select(orm.Job).where(orm.Job.id == job_id).with_for_update().execution_options(populate_existing=True)
    ).first()
    if job is None:
        raise NotFoundError("Job not found.")
    return job


def _cancel_locked(db: Session, job: orm.Job, now: datetime, cfg: SchedulerConfig) -> CancelResult:
    """Cancel a locked job whose cancel_reason is already set. Commits."""
    db.flush()                    # request_stop re-reads the locked row; write the reason first
    state = JobState(job.state)
    if state == JobState.QUEUED:
        job.state = JobState.CANCELLED
        job.state_reason = None
        db.commit()
        return CancelResult(job.id, JobState.CANCELLED, stop_pending=False)
    if state in (JobState.SCHEDULED, JobState.RUNNING):
        stop = request_stop(db, job.id, ReleaseReason.CANCELLED, now, cfg)     # commits
        return CancelResult(job.id, JobState.CANCELLED if stop.released else JobState.RUNNING,
                            stop_pending=not stop.released)
    raise ConflictError(f"Job {job.id} is already {state}.")
