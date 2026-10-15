"""
Cancelling a job (GPU requirements section 7.6; FR-GPU-065 to 069, UC-GPU-09).

    QUEUED      -> CANCELLED at once; no units to release (FR-GPU-066)
    SCHEDULED   -> CANCELLED and the claim rolled back in the same transaction (FR-GPU-067)
    RUNNING     -> the agent is asked to stop it (SIGTERM, stop_grace, SIGKILL; FR-GPU-068);
                   it becomes CANCELLED when the agent confirms, and only then are the
                   units released (FR-GPU-069)

The owner may cancel their own job; anyone else is refused with insufficient permission,
and the attempt is logged (FR-GPU-065). A Platform Admin cancelling any job with a
mandatory reason is PRAYOG-81, built on cancel_job and Job.cancel_reason.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import ConflictError, ForbiddenError, NotFoundError
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
        job = db.scalars(
            select(orm.Job).where(orm.Job.id == job_id).with_for_update().execution_options(populate_existing=True)
        ).first()
        if job is None:
            raise NotFoundError("Job not found.")
        if job.user_id != user_id:
            audit.warning("refused: user %s tried to cancel job %s owned by user %s", user_id, job_id, job.user_id)
            raise ForbiddenError("You do not have permission to cancel this job.")
        job.cancel_reason = "owner"
        db.flush()                    # request_stop re-reads the locked row; write the reason first
        state = JobState(job.state)
        if state == JobState.QUEUED:
            job.state = JobState.CANCELLED
            job.state_reason = None
            db.commit()
            result = CancelResult(job_id, JobState.CANCELLED, stop_pending=False)
        elif state in (JobState.SCHEDULED, JobState.RUNNING):
            stop = request_stop(db, job_id, ReleaseReason.CANCELLED, now, cfg)     # commits
            result = CancelResult(job_id, JobState.CANCELLED if stop.released else JobState.RUNNING,
                                  stop_pending=not stop.released)
        else:
            raise ConflictError(f"Job {job_id} is already {state}.")
    except BaseException:
        db.rollback()
        raise
    audit.info("job %s cancelled by its owner, user %s", job_id, user_id)
    return result
