"""
Rebuilding a recorded snapshot (FR-ML-003, Integrated Spec §8.4), interim version.

A rebuild is an ordinary job (FR-INT-01): it is submitted through submit_job() with
every scheduling rule, and the attempt's state is derived from the job's state, so the
two can never disagree:

    job IN_PROCESS/QUEUED/SCHEDULED/RUNNING   REBUILD_INITIATED
    job COMPLETED                              REBUILD_SUCCESS
    job FAILED                                 REBUILD_FAILED
    job CANCELLED or WALLTIME_EXCEEDED         REBUILD_CANCELLED

The snapshot is looked up in the Experiment Ledger first: the user's own RECORDED
snapshot whose code (and weights) still verify (logic.mlflow.rebuildable_snapshot).
A snapshot the ledger does not hold is still accepted if one of the user's own jobs
ran it, which keeps rebuilds of jobs submitted outside the ledger reachable. Anything
else is REBUILD_REJECTED: no attempt and no job, and the answer does not say whether
the snapshot exists (FR-ML-003, FR-AUTH-62).

The job carries "reproduces:<run_id>" and nothing else that differs from a first-time
submission: it is validated, classed, ranked and claimed like any job, with a fresh
submit time and no priority advantage (§14.3, ADR-08).
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import AppError, NotFoundError
from logic.mlflow import rebuildable_snapshot
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
    project_ref, reproduces = _resolve_snapshot(db, user_id, snapshot_hash)

    submission = submit_job(
        db, user_id, gpus=gpus, walltime=walltime, client_request_id=client_request_id,
        project_ref=project_ref, snapshot_hash=snapshot_hash, cfg=cfg, reproduces=reproduces,
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


def _resolve_snapshot(db: Session, user_id: int, snapshot_id: str) -> tuple[str | None, str]:
    """(project_ref, reproduces label) of a snapshot the user may rebuild, or REBUILD_REJECTED."""
    recorded = rebuildable_snapshot(db, snapshot_id, user_id)
    if recorded is not None:
        return recorded.project_id, f"reproduces:{recorded.run_id}"
    db.rollback()                         # end the ledger read before the scheduler locks rows
    original = db.scalars(
        select(orm.Job)
        .where(orm.Job.user_id == user_id, orm.Job.snapshot_hash == snapshot_id, orm.Job.reproduces.is_(None))
        .order_by(orm.Job.id)
    ).first()
    if original is None:
        raise RebuildRejected("Snapshot not found or not accessible.")
    run_id = db.scalar(select(orm.Run.id).where(orm.Run.job_id == original.id).order_by(orm.Run.id.desc()))
    return original.project_ref, f"reproduces:{run_id}" if run_id is not None else f"reproduces:job-{original.id}"


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
