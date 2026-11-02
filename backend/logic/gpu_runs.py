"""
GPU runs: one execution of a project version on the cluster ("A run 1", "A run 2", "B run 1", ...).

Submitting a run does three things that must agree:
  * a scheduler Job (GPUs, walltime) is queued through the ordinary submit_job(), with the version's
    manifest hash as its snapshot, so it is validated, ranked and claimed like any job;
  * an ML Flow ModelRun is created, numbered within its version and linked to that job;
  * the dataset's fingerprint (path, size, BLAKE3 per file) is recorded for the run, and each file must
    already be staged for a node to read.
A rebuild also records its RebuildAttempt (logic.rebuild), so its failure opens a Steward session (S3)
and its success can be optimised (S4), exactly as for POST /rebuilds (FR-STW-001, FR-STW-006, FR-INT-01).
Retrying with the same client_request_id returns the same run, never a second one.

The run's status follows the job: the node agent reports through the scheduler, and a release hook
(logic.mlflow_release) closes the ML run when the job ends.
"""

import json
from dataclasses import dataclass
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models as orm
from logic import project_versions
from logic.artifacts import get_verified_artifact
from logic.mlflow import rebuildable_snapshot
from logic.rebuild import RebuildRejected
from logic.errors import BadRequestError, ConflictError, NotFoundError
from logic.project_versions import version_label
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.requests import submit_job
from logic.staging import is_digest
from mlflow_models import (
    Artifact, Experiment, ModelRun, ModelSnapshot, Project, ProjectVersion, RunCheckpoint, RunDatasetFile, RunMetric, StagedFile,
)
from models import User

DEFAULT_ENTRYPOINT = "main.py"
MAX_DATASET_FILES = 20000
MAX_HYPERPARAMETER_BYTES = 64 * 1024


class DatasetNotStaged(ConflictError):
    """409 DATASET_NOT_STAGED: the files to upload first are named in the message."""
    code = "DATASET_NOT_STAGED"


class DatasetMismatch(ConflictError):
    """409 DATASET_MISMATCH: a rebuild must train on exactly the data the original recorded."""
    code = "DATASET_MISMATCH"


@dataclass(frozen=True)
class DatasetFile:
    path: str
    size_bytes: int
    blake3: str


def _safe_path(value) -> str:
    if not isinstance(value, str) or not value or "\\" in value or len(value) > 1024:
        raise BadRequestError("A dataset file path is invalid")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise BadRequestError(f"Unsafe dataset file path: {value}")
    return value


def check_dataset(files: list[DatasetFile]) -> list[DatasetFile]:
    if len(files) > MAX_DATASET_FILES:
        raise BadRequestError("Too many dataset files")
    seen = set()
    for item in files:
        _safe_path(item.path)
        if item.path in seen:
            raise BadRequestError(f"Dataset file listed twice: {item.path}")
        seen.add(item.path)
        if item.size_bytes < 0 or not is_digest(item.blake3):
            raise BadRequestError(f"Dataset file {item.path} needs a size and a BLAKE3 digest")
    return files


def _entrypoint_is_in_version(db: Session, user: User, version: ProjectVersion, entrypoint: str) -> None:
    if not entrypoint.endswith(".py"):
        raise BadRequestError("The entrypoint must be a Python file")
    _safe_path(entrypoint)
    _, path = get_verified_artifact(db, version.manifest_artifact_id, user.id, version.experiment_id, "CODE")
    files = json.loads(path.read_text(encoding="utf-8"))["files"]
    if not any(item.get("path") == entrypoint and item.get("included_in_archive") for item in files):
        raise BadRequestError(f"Version {version_label(version.version_number)} has no file {entrypoint}")


def _existing_run(db: Session, user: User, client_request_id: str) -> ModelRun | None:
    return db.query(ModelRun).join(orm.Job, orm.Job.id == ModelRun.job_id).filter(
        orm.Job.user_id == user.id, orm.Job.client_request_id == client_request_id).first()


def _parse_run_ref(ref: str) -> tuple[str, int]:
    version, _, number = (ref or "").strip().partition("/")
    if not version or not number.isdigit() or int(number) < 1:
        raise BadRequestError("Name the run to rebuild like A/1 (version letter, then run number).")
    return version, int(number)


def _source_for_rebuild(db: Session, user: User, project_id: str, ref: str) -> tuple[ModelRun, ModelSnapshot]:
    """The finished run to reproduce and its sealed snapshot, verified. Anything wrong with it, including that it
    does not exist or is someone else's, is the same REBUILD_REJECTED."""
    rejected = RebuildRejected("This run cannot be rebuilt: it was not found, or has no verified snapshot.")
    version, number = _parse_run_ref(ref)
    try:
        target = project_versions.get_version(db, user, project_id, version)
    except (NotFoundError, BadRequestError):
        raise rejected
    source = db.query(ModelRun).filter(ModelRun.project_version_id == target.id, ModelRun.run_number == number,
                                       ModelRun.user_id == user.id).first()
    snapshot = db.query(ModelSnapshot).filter(ModelSnapshot.run_id == source.id).first() if source else None
    if source is None or snapshot is None or rebuildable_snapshot(db, snapshot.snapshot_id, user.id) is None:
        raise rejected
    return source, snapshot


def _require_same_dataset(db: Session, source: ModelRun, dataset: list[DatasetFile]) -> None:
    recorded = {f.path: (f.size_bytes, f.blake3) for f in db.query(RunDatasetFile).filter(RunDatasetFile.run_id == source.id)}
    given = {f.path: (f.size_bytes, f.blake3) for f in dataset}
    missing = sorted(set(recorded) - set(given))
    extra = sorted(set(given) - set(recorded))
    changed = sorted(p for p in set(recorded) & set(given) if recorded[p] != given[p])
    if missing or extra or changed:
        parts = ([f"missing: {', '.join(missing)}"] if missing else []) + ([f"different from the original: {', '.join(changed)}"] if changed else []) \
            + ([f"not in the original: {', '.join(extra)}"] if extra else [])
        raise DatasetMismatch("A rebuild must use exactly the original run's data (checked by BLAKE3). " + "; ".join(parts))


def submit_gpu_run(
    db: Session, user: User, project_id: str, version: str | None, gpus, walltime, client_request_id: str,
    hyperparameters: dict | None = None, entrypoint: str | None = None,
    dataset: list[DatasetFile] | None = None, cfg: SchedulerConfig = DEFAULT_CONFIG,
    rebuild_of: str | None = None,
) -> tuple[ModelRun, bool]:
    """Queue a run of `version` (default: the project's current version). Returns (run, created).

    With rebuild_of ("A/1") the run reproduces that finished run: it takes the original's version, entrypoint and
    hyperparameters (none may be given), and its dataset must match the original's recorded fingerprint exactly."""
    if hyperparameters is not None and len(json.dumps(hyperparameters)) > MAX_HYPERPARAMETER_BYTES:
        raise BadRequestError("The hyperparameters are too large")
    dataset = check_dataset(dataset or [])

    existing = _existing_run(db, user, client_request_id)
    if existing is not None:                                   # a retry: no new run, whatever changed since
        return existing, False

    source = snapshot = None
    if rebuild_of:
        if version or entrypoint or hyperparameters:
            raise BadRequestError("A rebuild uses the original run's version, entrypoint and hyperparameters; do not give them.")
        source, snapshot = _source_for_rebuild(db, user, project_id, rebuild_of)
        _require_same_dataset(db, source, dataset)
        target = db.get(ProjectVersion, source.project_version_id)
        entrypoint, hyperparameters = source.environment.get("entrypoint", DEFAULT_ENTRYPOINT), dict(source.hyperparameters)
    else:
        hyperparameters, entrypoint = hyperparameters or {}, entrypoint or DEFAULT_ENTRYPOINT
        target = (project_versions.get_version(db, user, project_id, version) if version
                  else project_versions.current_version(db, user, project_id))
    _entrypoint_is_in_version(db, user, target, entrypoint)

    staged: dict[str, StagedFile] = {}
    if dataset:
        rows = db.query(StagedFile).filter(
            StagedFile.user_id == user.id, StagedFile.deleted_at.is_(None),
            StagedFile.blake3.in_([item.blake3 for item in dataset])).all()
        staged = {row.blake3: row for row in rows}
        absent = [item.path for item in dataset
                  if item.blake3 not in staged or staged[item.blake3].size_bytes != item.size_bytes]
        if absent:
            raise DatasetNotStaged("These dataset files must be uploaded first: " + ", ".join(absent[:20])
                                   + (f" (and {len(absent) - 20} more)" if len(absent) > 20 else ""))

    submission = submit_job(db, user.id, gpus=gpus, walltime=walltime, client_request_id=client_request_id,
                            project_ref=project_id, snapshot_hash=target.manifest_hash, cfg=cfg,
                            reproduces=f"reproduces:{source.id}" if source else None)
    job = submission.job
    run = db.query(ModelRun).filter(ModelRun.job_id == job.id).first()
    if run is not None:
        return run, False
    try:
        locked = db.query(ProjectVersion).filter(ProjectVersion.id == target.id).with_for_update().one()
        number = (db.query(func.max(ModelRun.run_number)).filter(ModelRun.project_version_id == locked.id).scalar() or 0) + 1
        project = db.get(Project, project_id)
        run = ModelRun(
            experiment_id=locked.experiment_id, project_id=project.project_id, model_id=_model_of(db, locked),
            user_id=user.id, run_type="REBUILD" if source else "FIRST_TIME", status="QUEUED",
            source_snapshot_id=snapshot.snapshot_id if snapshot else None, project_version_id=locked.id,
            run_number=number, job_id=job.id, hyperparameters=hyperparameters,
            environment={"entrypoint": entrypoint, "gpus": job.gpus, "walltime_minutes": job.walltime_minutes},
        )
        db.add(run)
        if source:                                             # committed with the run: one attempt per job
            db.add(orm.RebuildAttempt(user_id=user.id, snapshot_hash=snapshot.snapshot_id,
                                      project_ref=project_id, job_id=job.id))
        db.flush()
        db.add_all(RunDatasetFile(run_id=run.id, path=item.path, size_bytes=item.size_bytes, blake3=item.blake3,
                                  staged_file_id=staged[item.blake3].id) for item in dataset)
        db.commit()
    except IntegrityError:                                     # the same retry arrived twice at once
        db.rollback()
        run = db.query(ModelRun).filter(ModelRun.job_id == job.id).first()
        if run is None:
            raise
        return run, False
    db.refresh(run)
    return run, True


def _model_of(db: Session, version: ProjectVersion) -> str:
    return db.get(Experiment, version.experiment_id).model_id


# ── reading runs ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunView:
    run: ModelRun
    label: str                      # "A run 1"
    version: str                    # "A"
    job: orm.Job
    dataset: list[tuple[RunDatasetFile, bool]]        # (fingerprint, bytes still staged)
    checkpoints: list[tuple[RunCheckpoint, int]]    # (checkpoint, size in bytes)
    last_metrics: dict[str, float]
    snapshot_id: str | None
    rebuild_of: str | None          # "A run 1" when this run reproduces another


def _label(db: Session, run: ModelRun) -> str:
    return f"{version_label(db.get(ProjectVersion, run.project_version_id).version_number)} run {run.run_number}"


def _view(db: Session, run: ModelRun) -> RunView:
    version = db.get(ProjectVersion, run.project_version_id)
    letter = version_label(version.version_number)
    files = db.query(RunDatasetFile, StagedFile).outerjoin(StagedFile, StagedFile.id == RunDatasetFile.staged_file_id) \
        .filter(RunDatasetFile.run_id == run.id).order_by(RunDatasetFile.path).all()
    latest = {}
    for metric in db.query(RunMetric).filter(RunMetric.run_id == run.id).order_by(RunMetric.step, RunMetric.id):
        latest[metric.name] = metric.value
    snapshot = db.query(ModelSnapshot.snapshot_id).filter(ModelSnapshot.run_id == run.id).scalar()
    return RunView(
        run=run, label=f"{letter} run {run.run_number}", version=letter, job=db.get(orm.Job, run.job_id),
        dataset=[(f, staged is not None and staged.deleted_at is None) for f, staged in files],
        checkpoints=db.query(RunCheckpoint, Artifact.size_bytes).join(Artifact, Artifact.id == RunCheckpoint.artifact_id)
        .filter(RunCheckpoint.run_id == run.id).order_by(RunCheckpoint.step).all(),
        last_metrics=latest, snapshot_id=snapshot, rebuild_of=_original_label(db, run),
    )


def _original_label(db: Session, run: ModelRun) -> str | None:
    if run.run_type != "REBUILD" or run.source_snapshot_id is None:
        return None
    original = db.query(ModelRun).join(ModelSnapshot, ModelSnapshot.run_id == ModelRun.id) \
        .filter(ModelSnapshot.snapshot_id == run.source_snapshot_id).first()
    return _label(db, original) if original else None


def compare_rebuild(db: Session, user: User, project_id: str, version: str, number: int, tolerance: float = 0.0) -> dict:
    """A rebuild against the run it reproduces: every original metric within `tolerance`, and whether the saved
    weights are the very same bytes (equal BLAKE3 storage keys: a bit-exact reproduction)."""
    if not tolerance >= 0 or tolerance == float("inf"):
        raise BadRequestError("The tolerance must be a finite, non-negative number.")
    rebuilt_view = get_run(db, user, project_id, version, number)
    rebuilt = rebuilt_view.run
    if rebuilt.run_type != "REBUILD":
        raise BadRequestError(f"{rebuilt_view.label} is not a rebuild, so there is nothing to compare it with.")
    original = db.query(ModelRun).join(ModelSnapshot, ModelSnapshot.run_id == ModelRun.id) \
        .filter(ModelSnapshot.snapshot_id == rebuilt.source_snapshot_id).one()

    def metrics_of(run):
        return {(m.name, m.step): m.value for m in db.query(RunMetric).filter(RunMetric.run_id == run.id)}
    before, after = metrics_of(original), metrics_of(rebuilt)
    rows = []
    for (name, step), value in sorted(before.items()):
        got = after.get((name, step))
        difference = None if got is None else abs(got - value)
        rows.append({"name": name, "step": step, "original": value, "rebuilt": got, "difference": difference,
                     "within_tolerance": difference is not None and difference <= tolerance})

    def final_weights(run):
        snap = db.query(ModelSnapshot).filter(ModelSnapshot.run_id == run.id).first()
        artifact = db.get(Artifact, snap.weights_artifact_id) if snap and snap.weights_artifact_id else None
        if artifact is None and run.weights_artifact_id:
            artifact = db.get(Artifact, run.weights_artifact_id)
        return artifact.storage_key if artifact else None
    was, now = final_weights(original), final_weights(rebuilt)
    metrics_match = bool(rows) and all(r["within_tolerance"] for r in rows)
    return {
        "rebuilt": rebuilt_view.label, "original": _label(db, original), "rebuilt_status": rebuilt.status,
        "tolerance": tolerance, "metrics": rows, "metrics_match": metrics_match,
        "weights": {"original_blake3": was, "rebuilt_blake3": now, "identical": was is not None and was == now},
        "reproduced": rebuilt.status == "SUCCEEDED" and metrics_match,
    }


def list_runs(db: Session, user: User, project_id: str, version: str | None = None) -> list[RunView]:
    """Runs of the project, newest first; only a project's GPU runs (numbered ones)."""
    project_versions._owned_project(db, project_id, user)
    query = db.query(ModelRun).filter(ModelRun.project_id == project_id, ModelRun.user_id == user.id,
                                      ModelRun.project_version_id.is_not(None))
    if version:
        target = project_versions.get_version(db, user, project_id, version)
        query = query.filter(ModelRun.project_version_id == target.id)
    runs = query.order_by(ModelRun.created_at.desc(), ModelRun.id.desc()).all()
    return [_view(db, run) for run in runs]


def get_run(db: Session, user: User, project_id: str, version: str, number: int) -> RunView:
    target = project_versions.get_version(db, user, project_id, version)
    run = db.query(ModelRun).filter(ModelRun.project_version_id == target.id, ModelRun.run_number == number,
                                    ModelRun.user_id == user.id).first()
    if run is None:
        raise NotFoundError(f"Version {version_label(target.version_number)} has no run {number}.")
    return _view(db, run)

