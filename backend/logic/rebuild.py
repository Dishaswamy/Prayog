"""
Rebuilding a recorded snapshot (FR-ML-003, Integrated Spec §8.4), interim version.

A rebuild is an ordinary job (FR-INT-01): it is submitted through submit_job() with
every scheduling rule, and the attempt's state is derived from the job's state, so the
two can never disagree:

    job IN_PROCESS/QUEUED/SCHEDULED/RUNNING   REBUILD_INITIATED
    job COMPLETED                              REBUILD_SUCCESS
    job FAILED                                 REBUILD_FAILED
    job CANCELLED or WALLTIME_EXCEEDED         REBUILD_CANCELLED

REBUILD_REJECTED creates no attempt and no job. Until the ledger records snapshots, a
snapshot is accessible to a user if one of their own jobs ran it; anything else is
rejected without saying whether the snapshot exists (FR-ML-003, FR-AUTH-62).

The ledger sub-team owns this feature; this module keeps S3 and S4 reachable until
their snapshot store lands, and its API is what they are expected to keep.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import AppError, NotFoundError
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import JobState
from logic.scheduler.requests import submit_job

ATTEMPT_STATE = {
    JobState.IN_PROCESS: "REBUILD_INITIATED",
    JobState.QUEUED: "REBUILD_INITIATED",
    JobState.SCHEDULED: "REBUILD_INITIATED",
    JobState.RUNNING: "REBUILD_INITIATED",
    JobState.COMPLETED: "REBUILD_SUCCESS",
    JobState.FAILED: "REBUILD_FAILED",
    JobState.CANCELLED: "REBUILD_CANCELLED",
    JobState.WALLTIME_EXCEEDED: "REBUILD_CANCELLED",
}


class RebuildRejected(AppError):
    """REBUILD_REJECTED: no attempt was created (FR-STW-002: no Steward either)."""
    status_code = 404
    code = "REBUILD_REJECTED"


@dataclass(frozen=True)
class AttemptStatus:
    attempt: orm.RebuildAttempt
    job: orm.Job

    @property
    def state(self) -> str:
        return ATTEMPT_STATE[JobState(self.job.state)]


def start_rebuild(
    db: Session,
    user_id: int,
    snapshot_hash: str,
    gpus: Any,
    walltime: Any,
    client_request_id: str,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> tuple[AttemptStatus, bool]:
    """Validate the snapshot, submit the job, record the attempt. Idempotent per
    client_request_id: a retry returns the same attempt. Returns (status, created)."""
    original = db.scalars(
        select(orm.Job)
        .where(orm.Job.user_id == user_id, orm.Job.snapshot_hash == snapshot_hash)
        .order_by(orm.Job.id)
    ).first()
    if original is None:
        raise RebuildRejected("Snapshot not found or not accessible.")
    project_ref = original.project_ref

    submission = submit_job(
        db, user_id, gpus=gpus, walltime=walltime, client_request_id=client_request_id,
        project_ref=project_ref, snapshot_hash=snapshot_hash, cfg=cfg,
    )
    job = submission.job
    attempt = db.scalars(select(orm.RebuildAttempt).where(orm.RebuildAttempt.job_id == job.id)).first()
    created = attempt is None
    if attempt is None:
        attempt = orm.RebuildAttempt(
            user_id=user_id, snapshot_hash=snapshot_hash, project_ref=project_ref, job_id=job.id
        )
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
    db.refresh(job)
    return AttemptStatus(attempt, job), created


def get_attempt(db: Session, user_id: int, attempt_id: int) -> AttemptStatus:
    """The owner's view. Someone else's attempt is reported as not found."""
    row = db.execute(
        select(orm.RebuildAttempt, orm.Job)
        .join(orm.Job, orm.Job.id == orm.RebuildAttempt.job_id)
        .where(orm.RebuildAttempt.id == attempt_id)
    ).first()
    if row is None or row[0].user_id != user_id:
        raise NotFoundError("Rebuild attempt not found.")
    return AttemptStatus(row[0], row[1])


def list_attempts(db: Session, user_id: int) -> list[AttemptStatus]:
    rows = db.execute(
        select(orm.RebuildAttempt, orm.Job)
        .join(orm.Job, orm.Job.id == orm.RebuildAttempt.job_id)
        .where(orm.RebuildAttempt.user_id == user_id)
        .order_by(orm.RebuildAttempt.id.desc())
    ).all()
    return [AttemptStatus(a, j) for a, j in rows]
