"""
What a node agent may do for the GPU run on one of its allocations (architecture section 7.5).

An agent has no user login; its credential names its node, and everything here is scoped to a live
allocation on that node, so it can reach only the code and data of the run it was given and can
write only that run's results:

    workload       what to run: the version's files, the entrypoint, hyperparameters, dataset fingerprint
    code file      one chunk of the version's code archive, or its manifest (nothing else)
    data file      one staged dataset file of this run, checked against its BLAKE3 digest on read
    metrics        training metrics, while the run is RUNNING
    result file    a checkpoint (WEIGHTS at a step) or the run's log (LOG), stored as artifacts of the
                   run's experiment under the run's owner

Another node's allocation, a released one, or a job that is not a GPU run is reported as not found
or refused, never confirmed.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

import models as orm
from logic.artifact_storage import resolve_artifact
from logic.artifacts import get_verified_artifact, store_artifact
from logic.errors import BadRequestError, ConflictError, NotFoundError
from logic.mlflow import log_metrics
from logic.mlflow_release import sync_run
from logic.project_versions import version_label
from logic.scheduler.claim import utc_now
from mlflow_models import Artifact, Experiment, ModelRun, ProjectVersion, RunCheckpoint, RunDatasetFile, StagedFile
from models import User


@dataclass(frozen=True)
class Workload:
    run: ModelRun
    label: str
    entrypoint: str
    walltime_minutes: int
    manifest: dict
    manifest_artifact_id: str
    dataset: list[RunDatasetFile]


def _live_run(db: Session, node_id: int, allocation_id: int, started: bool = False) -> tuple[orm.Allocation, ModelRun]:
    allocation = db.get(orm.Allocation, allocation_id)
    if allocation is None or allocation.node_id != node_id:
        raise NotFoundError(f"Allocation {allocation_id} not found.")           # never confirm another node's IDs
    if allocation.released_at is not None:
        raise ConflictError(f"Allocation {allocation_id} is no longer live.")
    if started and allocation.acknowledged_at is None:
        raise ConflictError(f"Allocation {allocation_id} has not been started; acknowledge it first.")
    run = db.query(ModelRun).filter(ModelRun.job_id == allocation.job_id, ModelRun.project_version_id.is_not(None)).first()
    if run is None:
        raise NotFoundError(f"Allocation {allocation_id} carries no ML Flow run.")
    return allocation, run


def workload(db: Session, node_id: int, allocation_id: int) -> Workload:
    allocation, run = _live_run(db, node_id, allocation_id)
    version = db.get(ProjectVersion, run.project_version_id)
    owner = db.get(User, run.user_id)
    _, path = get_verified_artifact(db, version.manifest_artifact_id, owner.id, version.experiment_id, "CODE")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    job = db.get(orm.Job, allocation.job_id)
    dataset = db.query(RunDatasetFile).filter(RunDatasetFile.run_id == run.id).order_by(RunDatasetFile.path).all()
    return Workload(
        run=run, label=f"{version_label(version.version_number)} run {run.run_number}",
        entrypoint=run.environment.get("entrypoint", "main.py"), walltime_minutes=job.walltime_minutes,
        manifest=manifest, manifest_artifact_id=version.manifest_artifact_id, dataset=dataset,
    )


def code_file(db: Session, node_id: int, allocation_id: int, artifact_id: str) -> Path:
    """The manifest or one chunk of the run's version, verified against its recorded digests."""
    load = workload(db, node_id, allocation_id)
    allowed = {load.manifest_artifact_id} | {chunk.get("artifact_id") for chunk in load.manifest["chunks"]}
    if artifact_id not in allowed:
        raise NotFoundError("That file is not part of this run's code.")
    _, path = get_verified_artifact(db, artifact_id, load.run.user_id, load.run.experiment_id, "CODE")
    return path


def data_file(db: Session, node_id: int, allocation_id: int, digest: str) -> Path:
    """One staged dataset file of this run. The bytes' BLAKE3 digest is re-checked as they are resolved."""
    _, run = _live_run(db, node_id, allocation_id)
    row = db.query(RunDatasetFile, StagedFile).join(StagedFile, StagedFile.id == RunDatasetFile.staged_file_id) \
        .filter(RunDatasetFile.run_id == run.id, RunDatasetFile.blake3 == digest, StagedFile.deleted_at.is_(None)).first()
    if row is None:
        raise NotFoundError("That file is not available for this run (it is not part of the dataset, or it was deleted).")
    path = resolve_artifact(digest)
    if not path.is_file():
        raise NotFoundError("The staged file is missing.")
    return path


def _owner(db: Session, run: ModelRun) -> User:
    return db.get(User, run.user_id)


def record_metrics(db: Session, node_id: int, allocation_id: int, metrics: list[dict]) -> int:
    allocation, run = _live_run(db, node_id, allocation_id, started=True)
    if run.status == "QUEUED":
        sync_run(db, run, utc_now())
        db.commit()
    return len(log_metrics(db, run.id, _owner(db, run), metrics))


async def store_result(db: Session, node_id: int, allocation_id: int, kind: str, request,
                       step: int | None = None, best: bool = False) -> Artifact:
    """Store a checkpoint or the run's log. A checkpoint needs its step; a step is saved once."""
    if kind not in ("WEIGHTS", "LOG"):
        raise BadRequestError("A node may upload only WEIGHTS (a checkpoint) or LOG")
    if kind == "WEIGHTS" and (step is None or step < 0):
        raise BadRequestError("A checkpoint needs a non-negative step")
    allocation, run = _live_run(db, node_id, allocation_id, started=True)
    if run.status == "QUEUED":
        sync_run(db, run, utc_now())
        db.commit()
    if kind == "WEIGHTS" and db.query(RunCheckpoint.id).filter(
            RunCheckpoint.run_id == run.id, RunCheckpoint.step == step).first() is not None:
        raise ConflictError(f"This run already has a checkpoint at step {step}.")
    experiment = db.get(Experiment, run.experiment_id)
    artifact = await store_artifact(db, _owner(db, run), experiment, kind, request)
    if kind == "LOG":
        run.logs_artifact_id = artifact.id
        db.commit()
        return artifact
    if best:
        db.query(RunCheckpoint).filter(RunCheckpoint.run_id == run.id, RunCheckpoint.is_best).update({"is_best": False})
    db.add(RunCheckpoint(run_id=run.id, user_id=run.user_id, artifact_id=artifact.id, step=step, is_best=best))
    db.commit()
    return artifact
