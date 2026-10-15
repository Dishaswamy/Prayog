from datetime import datetime, timezone
from math import isfinite
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from logic.errors import BadRequestError, ConflictError, NotFoundError
from logic.mlflow_outbox import enqueue_rebuild_terminal_event, enqueue_run_sealed_event
from logic.artifacts import get_verified_artifact
from mlflow_models import Artifact, Experiment, MLModel, ModelRun, ModelSnapshot, Project, RebuildAttempt, RunMetric
from models import User

REBUILD_TERMINAL = {"REBUILD_SUCCESS", "REBUILD_FAILED", "REBUILD_CANCELLED"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _owned_project(db: Session, project_id: str, user: User) -> Project:
    item = db.query(Project).filter(Project.project_id == project_id, Project.user_id == user.id).first()
    if not item:
        raise NotFoundError("Project not found")
    return item


def _owned_model(db: Session, model_id: str, project_id: str, user: User) -> MLModel:
    item = db.query(MLModel).filter(
        MLModel.model_id == model_id,
        MLModel.project_id == project_id,
        MLModel.user_id == user.id,
    ).first()
    if not item:
        raise NotFoundError("Model not found")
    return item


def _owned_experiment(db: Session, experiment_id: int, user: User) -> Experiment:
    item = db.query(Experiment).filter(Experiment.id == experiment_id, Experiment.user_id == user.id).first()
    if not item:
        raise NotFoundError("Experiment not found")
    return item


def _owned_run(db: Session, run_id: int, user: User, lock: bool = False) -> ModelRun:
    query = db.query(ModelRun).filter(ModelRun.id == run_id, ModelRun.user_id == user.id)
    if lock:
        query = query.with_for_update()
    item = query.first()
    if not item:
        raise NotFoundError("Model run not found")
    return item


def _owned_snapshot(db: Session, snapshot_id: str, user: User) -> ModelSnapshot:
    item = db.query(ModelSnapshot).filter(ModelSnapshot.snapshot_id == snapshot_id, ModelSnapshot.user_id == user.id).first()
    if not item:
        raise NotFoundError("Snapshot not found")
    return item


def _owned_rebuild(db: Session, rebuild_id: int, user: User) -> RebuildAttempt:
    item = db.query(RebuildAttempt).filter(RebuildAttempt.id == rebuild_id, RebuildAttempt.user_id == user.id).first()
    if not item:
        raise NotFoundError("Rebuild attempt not found")
    return item


def create_project(db: Session, user: User, name: str, description: str | None) -> Project:
    item = Project(project_id=str(uuid4()), user_id=user.id, name=name.strip(), description=description)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_projects(db: Session, user: User) -> list[Project]:
    return db.query(Project).filter(Project.user_id == user.id).order_by(Project.updated_at.desc()).all()


def create_model(db: Session, project_id: str, user: User, name: str, description: str | None) -> MLModel:
    project = _owned_project(db, project_id, user)
    item = MLModel(
        model_id=str(uuid4()),
        project_id=project.project_id,
        user_id=user.id,
        name=name.strip(),
        description=description,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_models(db: Session, project_id: str, user: User) -> list[MLModel]:
    _owned_project(db, project_id, user)
    return db.query(MLModel).filter(
        MLModel.project_id == project_id,
        MLModel.user_id == user.id,
    ).order_by(MLModel.created_at.desc()).all()


def create_experiment(
    db: Session,
    user: User,
    project_id: str,
    model_id: str,
    name: str,
    description: str | None,
    tags: dict,
) -> Experiment:
    project = _owned_project(db, project_id, user)
    model = _owned_model(db, model_id, project.project_id, user)
    item = Experiment(
        project_id=project.project_id,
        model_id=model.model_id,
        user_id=user.id,
        name=name.strip(),
        description=description,
        tags=tags,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item

def list_experiments(db: Session, user: User) -> list[Experiment]:
    return db.query(Experiment).filter(Experiment.user_id == user.id).order_by(Experiment.updated_at.desc()).all()


def create_run(db: Session, experiment_id: int, user: User, hyperparameters: dict, environment: dict) -> ModelRun:
    experiment = _owned_experiment(db, experiment_id, user)
    item = ModelRun(
        experiment_id=experiment.id,
        project_id=experiment.project_id,
        model_id=experiment.model_id,
        user_id=user.id,
        run_type="FIRST_TIME",
        status="CREATED",
        hyperparameters=hyperparameters,
        environment=environment,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def get_run(db: Session, run_id: int, user: User) -> ModelRun:
    return _owned_run(db, run_id, user)


def list_runs(db: Session, experiment_id: int, user: User) -> list[ModelRun]:
    _owned_experiment(db, experiment_id, user)
    return db.query(ModelRun).filter(ModelRun.experiment_id == experiment_id, ModelRun.user_id == user.id).order_by(ModelRun.created_at.desc()).all()


def start_run(
    db: Session,
    run_id: int,
    user: User,
    environment: dict | None,
    code_artifact_id: str | None = None,
    weights_artifact_id: str | None = None,
) -> ModelRun:
    item = _owned_run(db, run_id, user, lock=True)
    if item.status != "CREATED":
        raise ConflictError("Only a CREATED run can be started")
    if weights_artifact_id and not code_artifact_id:
        raise BadRequestError("An input weights artifact requires a code artifact")
    code_artifact = None
    weights_artifact = None
    if code_artifact_id:
        code_artifact, _ = get_verified_artifact(
            db, code_artifact_id, user.id, item.experiment_id, "CODE"
        )
    if weights_artifact_id:
        weights_artifact, _ = get_verified_artifact(
            db, weights_artifact_id, user.id, item.experiment_id, "WEIGHTS"
        )
    item.status = "QUEUED" if code_artifact is not None else "RUNNING"
    item.started_at = None if code_artifact is not None else _now()
    if environment is not None:
        item.environment = environment
    if code_artifact is not None:
        item.code_artifact_id = code_artifact.id
        item.code_snapshot_uri = f"artifact:{code_artifact.id}"
        item.code_snapshot_sha256 = code_artifact.sha256
    if weights_artifact is not None:
        item.weights_artifact_id = weights_artifact.id
        item.weights_uri = f"artifact:{weights_artifact.id}"
    db.commit()
    db.refresh(item)
    return item


def finish_run(
    db: Session,
    run_id: int,
    user: User,
    result: dict,
    worker_id: str | None = None,
) -> ModelRun:
    item = _owned_run(db, run_id, user, lock=True)
    if item.status != "RUNNING":
        raise ConflictError("Only a RUNNING run can be completed")
    if item.run_type == "FIRST_TIME" and item.code_artifact_id is not None:
        if item.worker_id is None or item.worker_id != worker_id:
            raise ConflictError("This first-time run is queued for worker execution")
    elif item.worker_id is not None and item.worker_id != worker_id:
        raise ConflictError("This run is currently managed by an execution worker")
    status = result["status"]
    code_artifact_id = result.get("code_artifact_id") or item.code_artifact_id
    weights_artifact_id = result.get("weights_artifact_id") or item.weights_artifact_id
    logs_artifact_id = result.get("logs_artifact_id")
    code_artifact = None
    weights_artifact = None
    logs_artifact = None
    warnings = []

    if code_artifact_id:
        try:
            code_artifact, _ = get_verified_artifact(
                db, code_artifact_id, user.id, item.experiment_id, "CODE"
            )
        except (BadRequestError, NotFoundError, OSError):
            warnings.append("Code snapshot artifact was unavailable or failed integrity verification.")
    else:
        warnings.append("No code snapshot artifact was supplied.")

    if weights_artifact_id:
        try:
            weights_artifact, _ = get_verified_artifact(
                db, weights_artifact_id, user.id, item.experiment_id, "WEIGHTS"
            )
        except (BadRequestError, NotFoundError, OSError):
            warnings.append("Weights artifact was unavailable or failed integrity verification.")

    if logs_artifact_id:
        try:
            logs_artifact, _ = get_verified_artifact(
                db, logs_artifact_id, user.id, item.experiment_id, "LOG"
            )
        except (BadRequestError, NotFoundError, OSError):
            warnings.append("Log artifact was unavailable or failed integrity verification.")

    expected_metrics = item.environment.get("expected_metrics", [])
    expected_metrics_invalid = not isinstance(expected_metrics, list)
    if isinstance(expected_metrics, list):
        expected_metrics_invalid = any(
            not isinstance(name, str) or not name.strip() or len(name.strip()) > 100
            for name in expected_metrics
        )
        expected_metrics = [name.strip() for name in expected_metrics if isinstance(name, str) and name.strip()]
    if expected_metrics_invalid:
        warnings.append("Invalid expected_metrics declaration; expected a list of non-blank metric names.")
    if expected_metrics:
        recorded_metrics = {
            row[0] for row in db.query(RunMetric.name).filter(
                RunMetric.run_id == item.id,
                RunMetric.user_id == user.id,
            ).distinct().all()
        }
        missing_metrics = sorted(set(expected_metrics) - recorded_metrics)
        if missing_metrics:
            warnings.append("Missing expected metrics: " + ", ".join(missing_metrics))

    snapshot_complete = code_artifact is not None and (
        not weights_artifact_id or weights_artifact is not None
    ) and (not logs_artifact_id or logs_artifact is not None) and not expected_metrics_invalid and not (
        any(warning.startswith("Missing expected metrics:") for warning in warnings)
    )
    snapshot_reasons = list(warnings)
    snapshot = ModelSnapshot(
        snapshot_id=str(uuid4()),
        experiment_id=item.experiment_id,
        project_id=item.project_id,
        model_id=item.model_id,
        run_id=item.id,
        user_id=user.id,
        state="RECORDED" if snapshot_complete else "INCOMPLETE",
        code_uri=f"artifact:{code_artifact.id}" if code_artifact else None,
        code_sha256=code_artifact.sha256 if code_artifact else None,
        dependencies=item.environment,
        hyperparameters=item.hyperparameters,
        completeness_reasons=snapshot_reasons,
        weights_uri=f"artifact:{weights_artifact.id}" if weights_artifact else None,
        code_artifact_id=code_artifact.id if code_artifact else None,
        weights_artifact_id=weights_artifact.id if weights_artifact else None,
        logs_artifact_id=logs_artifact.id if logs_artifact else None,
    )
    db.add(snapshot)
    if snapshot.state == "RECORDED":
        enqueue_run_sealed_event(db, snapshot, item, user.id)

    item.status = status
    item.code_artifact_id = code_artifact.id if code_artifact else None
    item.weights_artifact_id = weights_artifact.id if weights_artifact else None
    item.logs_artifact_id = logs_artifact.id if logs_artifact else None
    item.code_snapshot_uri = f"artifact:{code_artifact.id}" if code_artifact else None
    item.code_snapshot_sha256 = code_artifact.sha256 if code_artifact else None
    item.weights_uri = f"artifact:{weights_artifact.id}" if weights_artifact else None
    item.logs_uri = f"artifact:{logs_artifact.id}" if logs_artifact else None
    item.logging_state = "DEGRADED" if warnings else "COMPLETE"
    item.logging_warnings = warnings
    item.error_summary = result.get("error_summary")
    item.finished_at = _now()
    if status == "FAILED" and not item.error_summary:
        item.error_summary = "Run failed; no error summary was supplied."
    db.commit()
    db.refresh(item)
    return item

def log_metric(db: Session, run_id: int, user: User, name: str, value: float, step: int) -> RunMetric:
    item = _owned_run(db, run_id, user, lock=True)
    if item.status != "RUNNING":
        raise ConflictError("Metrics can only be logged while a run is RUNNING")
    name = name.strip()
    if not name or len(name) > 100:
        raise BadRequestError("Metric name must contain between 1 and 100 characters")
    if not isfinite(value):
        raise BadRequestError("Metric value must be finite")
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        raise BadRequestError("Metric step must be a non-negative integer")
    metric = RunMetric(run_id=item.id, user_id=user.id, name=name, value=value, step=step)
    db.add(metric)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("A metric with this name and step already exists for the run") from exc
    db.refresh(metric)
    return metric


def log_metrics(db: Session, run_id: int, user: User, values: list[dict]) -> list[RunMetric]:
    item = _owned_run(db, run_id, user, lock=True)
    if item.status != "RUNNING":
        raise ConflictError("Metrics can only be logged while a run is RUNNING")
    if not values or len(values) > 500:
        raise BadRequestError("A metric batch must contain between 1 and 500 metrics")

    normalized = []
    seen = set()
    for value in values:
        name = value["name"].strip()
        step = value.get("step", 0)
        metric_value = value["value"]
        if not name or len(name) > 100:
            raise BadRequestError("Metric name must contain between 1 and 100 characters")
        if not isfinite(metric_value):
            raise BadRequestError("Metric value must be finite")
        if not isinstance(step, int) or isinstance(step, bool) or step < 0:
            raise BadRequestError("Metric step must be a non-negative integer")
        key = (name, step)
        if key in seen:
            raise ConflictError("A metric with this name and step appears more than once in the batch")
        seen.add(key)
        normalized.append((name, metric_value, step))

    metrics = [
        RunMetric(run_id=item.id, user_id=user.id, name=name, value=metric_value, step=step)
        for name, metric_value, step in normalized
    ]
    db.add_all(metrics)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("A metric with this name and step already exists for the run") from exc
    for metric in metrics:
        db.refresh(metric)
    return metrics


def list_metrics(db: Session, run_id: int, user: User) -> list[RunMetric]:
    _owned_run(db, run_id, user)
    return db.query(RunMetric).filter(RunMetric.run_id == run_id, RunMetric.user_id == user.id).order_by(RunMetric.recorded_at, RunMetric.id).all()


def list_snapshots(db: Session, experiment_id: int, user: User) -> list[ModelSnapshot]:
    _owned_experiment(db, experiment_id, user)
    return db.query(ModelSnapshot).filter(ModelSnapshot.experiment_id == experiment_id, ModelSnapshot.user_id == user.id).order_by(ModelSnapshot.created_at.desc()).all()


def get_snapshot(db: Session, snapshot_id: str, user: User) -> ModelSnapshot:
    return _owned_snapshot(db, snapshot_id, user)


def rebuildable_snapshot(db: Session, snapshot_id: str, user_id: int) -> ModelSnapshot | None:
    """The user's RECORDED snapshot with verified code (and weights), or None. Missing, foreign,
    incomplete and corrupt snapshots are indistinguishable, so callers can answer with one
    pre-init rejection that does not disclose whether the snapshot exists."""
    snapshot = db.query(ModelSnapshot).filter(
        ModelSnapshot.snapshot_id == snapshot_id,
        ModelSnapshot.user_id == user_id,
    ).first()
    if snapshot is None or snapshot.state != "RECORDED":
        return None
    try:
        code_artifact, _ = get_verified_artifact(
            db, snapshot.code_artifact_id, user_id, snapshot.experiment_id, "CODE"
        )
        if snapshot.code_sha256 and code_artifact.sha256 != snapshot.code_sha256:
            return None
        if snapshot.weights_artifact_id:
            get_verified_artifact(
                db, snapshot.weights_artifact_id, user_id, snapshot.experiment_id, "WEIGHTS"
            )
    except Exception:
        return None
    return snapshot


def create_rebuild(db: Session, snapshot_id: str, user: User, environment: dict) -> RebuildAttempt:
    snapshot = rebuildable_snapshot(db, snapshot_id, user.id)
    if snapshot is None:
        raise BadRequestError("REBUILD_REJECTED: snapshot could not be validated")
    rebuild_environment = {
        **(environment or snapshot.dependencies),
        "scheduler_label": f"reproduces:{snapshot.run_id}",
    }
    execution = ModelRun(
        experiment_id=snapshot.experiment_id,
        project_id=snapshot.project_id,
        model_id=snapshot.model_id,
        user_id=user.id,
        run_type="REBUILD",
        source_snapshot_id=snapshot.snapshot_id,
        status="RUNNING",
        hyperparameters=snapshot.hyperparameters,
        environment=rebuild_environment,
        code_snapshot_uri=snapshot.code_uri,
        code_snapshot_sha256=snapshot.code_sha256,
        code_artifact_id=snapshot.code_artifact_id,
        started_at=_now(),
    )
    db.add(execution)
    db.flush()
    item = RebuildAttempt(
        user_id=user.id,
        snapshot_id=snapshot.snapshot_id,
        project_id=snapshot.project_id,
        model_id=snapshot.model_id,
        execution_run_id=execution.id,
        state="REBUILD_INITIATED",
        environment=rebuild_environment,
        initiated_at=_now(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
def update_rebuild(
    db: Session,
    rebuild_id: int,
    user: User,
    state: str,
    logs_artifact_id: str | None,
    weights_artifact_id: str | None,
    failure_summary: str | None,
    worker_id: str | None = None,
) -> RebuildAttempt:
    item = db.query(RebuildAttempt).filter(
        RebuildAttempt.id == rebuild_id,
        RebuildAttempt.user_id == user.id,
    ).with_for_update().first()
    if item is None:
        raise NotFoundError("Rebuild attempt not found")
    if item.state in REBUILD_TERMINAL:
        raise ConflictError("A terminal rebuild attempt cannot be changed; create a new attempt")
    if item.state != "REBUILD_INITIATED":
        raise ConflictError("Only an initiated rebuild can finish")
    claimed_by = getattr(item, "worker_id", None)
    if claimed_by is not None and claimed_by != worker_id:
        raise ConflictError("This rebuild is currently managed by an execution worker")
    if state not in REBUILD_TERMINAL:
        raise BadRequestError("Unsupported rebuild state")
    if state == "REBUILD_FAILED" and not failure_summary:
        raise BadRequestError("A failed rebuild must include a failure summary")
    if state == "REBUILD_CANCELLED" and weights_artifact_id:
        raise BadRequestError("Cancelled rebuilds cannot register final weights")

    snapshot = _owned_snapshot(db, item.snapshot_id, user)
    logging_warnings = []
    log_artifact = None
    if logs_artifact_id:
        try:
            log_artifact, _ = get_verified_artifact(
                db, logs_artifact_id, user.id, snapshot.experiment_id, "LOG"
            )
        except (BadRequestError, NotFoundError):
            logging_warnings.append("Rebuild log artifact was unavailable or failed integrity verification.")

    source_code_artifact = None
    output_weights_artifact = None
    if state in {"REBUILD_SUCCESS", "REBUILD_FAILED"}:
        try:
            source_code_artifact, _ = get_verified_artifact(
                db, snapshot.code_artifact_id, user.id, snapshot.experiment_id, "CODE"
            )
            if snapshot.code_sha256 and source_code_artifact.sha256 != snapshot.code_sha256:
                raise BadRequestError("Snapshot code digest does not match its artifact")
            if state == "REBUILD_SUCCESS" and snapshot.weights_artifact_id:
                get_verified_artifact(
                    db, snapshot.weights_artifact_id, user.id, snapshot.experiment_id, "WEIGHTS"
                )
        except (BadRequestError, NotFoundError):
            source_code_artifact = None
            logging_warnings.append("Source snapshot artifact was unavailable or failed integrity verification.")
            if state == "REBUILD_SUCCESS":
                state = "REBUILD_FAILED"
                failure_summary = "A required source artifact became unavailable or failed integrity verification during rebuild."

        if weights_artifact_id:
            try:
                output_weights_artifact, _ = get_verified_artifact(
                    db, weights_artifact_id, user.id, snapshot.experiment_id, "WEIGHTS"
                )
            except (BadRequestError, NotFoundError):
                output_weights_artifact = None
                logging_warnings.append("Rebuild weights artifact was unavailable or failed integrity verification.")
                if state == "REBUILD_SUCCESS":
                    state = "REBUILD_FAILED"
                    failure_summary = "A required output artifact became unavailable or failed integrity verification during rebuild."

    execution = db.query(ModelRun).filter(
        ModelRun.id == item.execution_run_id,
        ModelRun.user_id == user.id,
    ).with_for_update().first()
    if execution is None:
        raise NotFoundError("Rebuild execution record not found")
    if execution.status != "RUNNING":
        raise ConflictError("The rebuild execution is already terminal")

    now = _now()
    item.state = state
    item.finished_at = now
    item.logs_artifact_id = log_artifact.id if log_artifact else None
    item.logs_uri = f"artifact:{log_artifact.id}" if log_artifact else None
    item.failure_summary = failure_summary if state == "REBUILD_FAILED" else None
    execution.status = {
        "REBUILD_SUCCESS": "SUCCEEDED",
        "REBUILD_FAILED": "FAILED",
        "REBUILD_CANCELLED": "CANCELLED",
    }[state]
    execution.finished_at = now
    execution.logs_artifact_id = log_artifact.id if log_artifact else None
    execution.logs_uri = f"artifact:{log_artifact.id}" if log_artifact else None
    execution.error_summary = failure_summary if state == "REBUILD_FAILED" else None
    execution.logging_state = "DEGRADED" if logging_warnings else "COMPLETE"
    execution.logging_warnings = logging_warnings

    if state != "REBUILD_CANCELLED" and source_code_artifact is not None:
        db.add(ModelSnapshot(
            snapshot_id=str(uuid4()),
            experiment_id=snapshot.experiment_id,
            project_id=snapshot.project_id,
            model_id=snapshot.model_id,
            run_id=execution.id,
            user_id=user.id,
            state="RECORDED",
            code_uri=f"artifact:{source_code_artifact.id}",
            code_sha256=source_code_artifact.sha256,
            dependencies=item.environment,
            hyperparameters=execution.hyperparameters,
            weights_uri=f"artifact:{output_weights_artifact.id}" if output_weights_artifact else None,
            code_artifact_id=source_code_artifact.id,
            weights_artifact_id=output_weights_artifact.id if output_weights_artifact else None,
            logs_artifact_id=log_artifact.id if log_artifact else None,
        ))
        execution.code_artifact_id = source_code_artifact.id
        execution.code_snapshot_uri = f"artifact:{source_code_artifact.id}"
        execution.code_snapshot_sha256 = source_code_artifact.sha256
        execution.weights_artifact_id = output_weights_artifact.id if output_weights_artifact else None
        execution.weights_uri = f"artifact:{output_weights_artifact.id}" if output_weights_artifact else None

    enqueue_rebuild_terminal_event(
        db, item, snapshot, execution, user.id, state, log_artifact
    )
    db.commit()
    db.refresh(item)
    return item

def get_rebuild(db: Session, rebuild_id: int, user: User) -> RebuildAttempt:
    return _owned_rebuild(db, rebuild_id, user)


def list_rebuilds(db: Session, snapshot_id: str, user: User) -> list[RebuildAttempt]:
    _owned_snapshot(db, snapshot_id, user)
    return db.query(RebuildAttempt).filter(RebuildAttempt.snapshot_id == snapshot_id, RebuildAttempt.user_id == user.id).order_by(RebuildAttempt.created_at.desc()).all()
