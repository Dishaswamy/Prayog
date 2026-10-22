"""
Keeping a GPU run's ML Flow record in step with its scheduler job.

The node agent reports through the scheduler, which owns the job's state. This module follows it:

    job RUNNING                    run RUNNING
    job back in the queue          run QUEUED again, its partial metrics and checkpoints discarded
                                   (the platform stopped it; it restarts from the beginning)
    job COMPLETED                  run SUCCEEDED, and its snapshot is sealed
    job FAILED / WALLTIME_EXCEEDED run FAILED, with a reason; checkpoints and metrics are kept
    job CANCELLED                  run CANCELLED

sync_run() applies one change and is idempotent. It runs inside the scheduler's release transaction
(register_mlflow_hooks), so the run and the job commit together; and reconcile_runs() applies it to any
run a path without a release missed (a queued job cancelled, a decommission), from the scheduler tick.
A hook failing never blocks a release (lifecycle._run_release_hooks).
"""

import logging
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import models as orm
from logic.mlflow_outbox import enqueue_run_sealed_event
from logic.project_versions import version_label
from logic.scheduler.claim import utc_now
from logic.scheduler.domain import JobState, ReleaseReason
from logic.scheduler.lifecycle import ReleaseResult, register_release_hook
from mlflow_models import Artifact, ModelRun, ModelSnapshot, ProjectVersion, RunCheckpoint, RunDatasetFile, RunMetric

log = logging.getLogger("prayog.mlflow")

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
OUTCOME = {
    JobState.COMPLETED: "SUCCEEDED",
    JobState.FAILED: "FAILED",
    JobState.WALLTIME_EXCEEDED: "FAILED",
    JobState.CANCELLED: "CANCELLED",
}
LOG_EXCERPT = 400


def _failure_summary(session: Session, job: orm.Job) -> str:
    if job.state == JobState.WALLTIME_EXCEEDED:
        return f"Stopped at the walltime limit ({job.walltime_minutes // 60:02d}:{job.walltime_minutes % 60:02d}). " \
               "Checkpoints saved before then are kept."
    last = session.scalars(select(orm.Run).where(orm.Run.job_id == job.id).order_by(orm.Run.id.desc()).limit(1)).first()
    if last is None:
        return "The run could not be started on a node."
    summary = f"The program exited with code {last.exit_code}." if last.exit_code is not None else "The run failed."
    tail = session.scalar(select(orm.RunLog.log_tail).where(orm.RunLog.run_id == last.id))
    if tail and tail.strip():
        summary += " Last output: " + tail.strip()[-LOG_EXCERPT:]
    return summary


def _best_checkpoint(session: Session, run: ModelRun) -> RunCheckpoint | None:
    rows = session.query(RunCheckpoint).filter(RunCheckpoint.run_id == run.id).order_by(RunCheckpoint.step).all()
    return next((c for c in rows if c.is_best), rows[-1] if rows else None)


def _seal(session: Session, run: ModelRun, version: ProjectVersion, manifest: Artifact) -> None:
    if session.query(ModelSnapshot.id).filter(ModelSnapshot.run_id == run.id).first() is not None:
        return
    chosen = _best_checkpoint(session, run)
    dataset = [{"path": f.path, "size_bytes": f.size_bytes, "blake3": f.blake3} for f in
               session.query(RunDatasetFile).filter(RunDatasetFile.run_id == run.id).order_by(RunDatasetFile.path)]
    snapshot = ModelSnapshot(
        snapshot_id=str(uuid4()), experiment_id=run.experiment_id, project_id=run.project_id, model_id=run.model_id,
        run_id=run.id, user_id=run.user_id, state="RECORDED",
        code_uri=f"artifact:{manifest.id}", code_sha256=manifest.sha256, code_artifact_id=manifest.id,
        weights_artifact_id=chosen.artifact_id if chosen else None,
        weights_uri=f"artifact:{chosen.artifact_id}" if chosen else None,
        logs_artifact_id=run.logs_artifact_id,
        dependencies={**run.environment, "project_version": version_label(version.version_number),
                      "manifest_hash": version.manifest_hash, "dataset": dataset},
        hyperparameters=run.hyperparameters, completeness_reasons=[],
    )
    session.add(snapshot)
    session.flush()
    enqueue_run_sealed_event(session, snapshot, run, run.user_id)


def sync_run(session: Session, run: ModelRun, now: datetime) -> bool:
    """Bring the run in line with its job. Returns True if anything changed."""
    job = session.get(orm.Job, run.job_id)
    state = JobState(job.state)
    if state in OUTCOME:
        if run.status in TERMINAL:
            return False
        outcome = OUTCOME[state]
        version = session.get(ProjectVersion, run.project_version_id)
        manifest = session.get(Artifact, version.manifest_artifact_id)
        started = session.scalar(select(func.min(orm.Run.started_at)).where(orm.Run.job_id == job.id))
        run.status = outcome
        run.started_at = run.started_at or started
        run.finished_at = now
        run.code_artifact_id, run.code_snapshot_uri, run.code_snapshot_sha256 = (
            manifest.id, f"artifact:{manifest.id}", manifest.sha256)
        chosen = _best_checkpoint(session, run)
        if chosen is not None:
            run.weights_artifact_id, run.weights_uri = chosen.artifact_id, f"artifact:{chosen.artifact_id}"
        if run.logs_artifact_id:
            run.logs_uri = f"artifact:{run.logs_artifact_id}"
        if outcome == "FAILED":
            run.error_summary = _failure_summary(session, job)
        elif outcome == "CANCELLED":
            run.error_summary = "Cancelled before it finished."
        if outcome == "SUCCEEDED":
            _seal(session, run, version, manifest)
        session.flush()
        return True
    if state == JobState.QUEUED and run.status == "RUNNING":
        session.query(RunMetric).filter(RunMetric.run_id == run.id).delete(synchronize_session=False)
        session.query(RunCheckpoint).filter(RunCheckpoint.run_id == run.id).delete(synchronize_session=False)
        run.status, run.started_at, run.logs_artifact_id, run.logs_uri = "QUEUED", None, None, None
        run.weights_artifact_id = run.weights_uri = None
        session.flush()
        return True
    if state == JobState.RUNNING and run.status == "QUEUED":
        run.status = "RUNNING"
        run.started_at = session.scalar(select(func.min(orm.Run.started_at)).where(orm.Run.job_id == job.id)) or now
        session.flush()
        return True
    return False


def release_hook(session: Session, result: ReleaseResult, reason: ReleaseReason, now: datetime) -> None:
    run = session.query(ModelRun).filter(ModelRun.job_id == result.job_id, ModelRun.project_version_id.is_not(None)) \
        .with_for_update().first()
    if run is not None:
        sync_run(session, run, now)


def sync_allocation_run(session: Session, allocation_id: int) -> None:
    """The agent has just started this allocation: show its run as RUNNING."""
    allocation = session.get(orm.Allocation, allocation_id)
    run = session.query(ModelRun).filter(ModelRun.job_id == allocation.job_id,
                                         ModelRun.project_version_id.is_not(None)).with_for_update().first() \
        if allocation is not None else None
    if run is not None and sync_run(session, run, utc_now()):
        session.commit()


def displayed_status(run: ModelRun, job_state: str) -> str:
    """The run's status as a user should see it: a finished job shows its outcome even before the sweep records it."""
    if run.status in TERMINAL:
        return run.status
    return OUTCOME.get(JobState(job_state), run.status)


def reconcile_runs(session: Session, now: datetime | None = None) -> int:
    """Sync every unfinished GPU run whose job has moved on without a release (a queued job cancelled)."""
    now = now or utc_now()
    changed = 0
    runs = session.query(ModelRun).filter(ModelRun.project_version_id.is_not(None), ModelRun.status.not_in(TERMINAL)) \
        .with_for_update(skip_locked=True).all()
    for run in runs:
        try:
            with session.begin_nested():
                changed += sync_run(session, run, now)
        except Exception:
            log.exception("could not reconcile run %s", run.id)
    session.commit()
    return changed


def register_mlflow_hooks() -> None:
    """Every process that releases allocations (the API, the scheduler tick) must call this, so a job's
    end reaches its ML Flow run."""
    register_release_hook(release_hook)
