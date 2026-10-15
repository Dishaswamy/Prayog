"""Read-only ML Flow access for Professors with an active approved relationship.

A relationship is represented by an approved Student or Researcher row. Student
relationships expire at end_date; Researcher relationships remain active until
their approved row is removed. Every lookup is scoped to those related users so
an unrelated resource is indistinguishable from a missing resource.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from logic.errors import ForbiddenError, NotFoundError
from mlflow_models import (
    Artifact,
    Experiment,
    MLModel,
    ModelRun,
    ModelSnapshot,
    Project,
    RebuildAttempt,
    RunMetric,
)
from models import Professor, Researcher, Student, User


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _related_user_ids(db: Session, professor_user: User) -> list[int]:
    if professor_user.role != "PROFESSOR":
        raise ForbiddenError("Professor access is required")

    professor = (
        db.query(Professor)
        .filter(Professor.user_id == professor_user.id)
        .first()
    )
    if professor is None:
        raise NotFoundError("Professor profile not found")

    now = _now_naive_utc()
    active_students = (
        db.query(Student)
        .filter(
            Student.professor_id == professor.id,
            Student.start_date <= now,
            Student.end_date >= now,
        )
        .all()
    )
    active_researchers = (
        db.query(Researcher)
        .filter(
            Researcher.professor_id == professor.id,
            Researcher.start_date <= now,
        )
        .all()
    )
    return sorted({item.user_id for item in [*active_students, *active_researchers]})


def _related_project(db: Session, project_id: str, user_ids: list[int]) -> Project:
    project = (
        db.query(Project)
        .filter(Project.project_id == project_id, Project.user_id.in_(user_ids))
        .first()
    )
    if project is None:
        raise NotFoundError("Project not found")
    return project


def _related_experiment(db: Session, experiment_id: int, user_ids: list[int]) -> Experiment:
    experiment = (
        db.query(Experiment)
        .filter(Experiment.id == experiment_id, Experiment.user_id.in_(user_ids))
        .first()
    )
    if experiment is None:
        raise NotFoundError("Experiment not found")
    return experiment


def _artifact_is_consistent(
    db: Session, artifact_id: str | None, *, user_id: int, experiment_id: int,
    project_id: str, model_id: str, expected_kind: str,
) -> bool:
    """Validate an optional artifact reference without disclosing its storage location."""
    if artifact_id is None:
        return True
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    return bool(
        artifact is not None
        and artifact.user_id == user_id
        and artifact.experiment_id == experiment_id
        and artifact.project_id == project_id
        and artifact.model_id == model_id
        and artifact.kind == expected_kind
    )


def _run_is_consistent(db: Session, run: ModelRun) -> bool:
    """Reject dangling or cross-owner run relationships before Professor disclosure."""
    experiment = db.query(Experiment).filter(Experiment.id == run.experiment_id).first()
    project = db.query(Project).filter(Project.project_id == run.project_id).first()
    model = db.query(MLModel).filter(MLModel.model_id == run.model_id).first()
    parents_match = bool(
        experiment is not None
        and project is not None
        and model is not None
        and experiment.user_id == run.user_id
        and experiment.project_id == run.project_id
        and experiment.model_id == run.model_id
        and project.user_id == run.user_id
        and model.user_id == run.user_id
        and model.project_id == run.project_id
    )
    return parents_match and all(
        _artifact_is_consistent(
            db, artifact_id, user_id=run.user_id, experiment_id=run.experiment_id,
            project_id=run.project_id, model_id=run.model_id, expected_kind=expected_kind,
        )
        for artifact_id, expected_kind in (
            (run.code_artifact_id, "CODE"),
            (run.weights_artifact_id, "WEIGHTS"),
            (run.logs_artifact_id, "LOG"),
        )
    )


def _snapshot_is_consistent(db: Session, snapshot: ModelSnapshot) -> bool:
    """Require the snapshot, its source run, and all parent/artifact records to agree."""
    experiment = db.query(Experiment).filter(Experiment.id == snapshot.experiment_id).first()
    project = db.query(Project).filter(Project.project_id == snapshot.project_id).first()
    model = db.query(MLModel).filter(MLModel.model_id == snapshot.model_id).first()
    run = db.query(ModelRun).filter(ModelRun.id == snapshot.run_id).first()
    parents_match = bool(
        experiment is not None
        and project is not None
        and model is not None
        and run is not None
        and experiment.user_id == snapshot.user_id
        and experiment.project_id == snapshot.project_id
        and experiment.model_id == snapshot.model_id
        and project.user_id == snapshot.user_id
        and model.user_id == snapshot.user_id
        and model.project_id == snapshot.project_id
        and run.user_id == snapshot.user_id
        and run.experiment_id == snapshot.experiment_id
        and run.project_id == snapshot.project_id
        and run.model_id == snapshot.model_id
        and _run_is_consistent(db, run)
    )
    return parents_match and all(
        _artifact_is_consistent(
            db, artifact_id, user_id=snapshot.user_id, experiment_id=snapshot.experiment_id,
            project_id=snapshot.project_id, model_id=snapshot.model_id, expected_kind=expected_kind,
        )
        for artifact_id, expected_kind in (
            (snapshot.code_artifact_id, "CODE"),
            (snapshot.weights_artifact_id, "WEIGHTS"),
            (snapshot.logs_artifact_id, "LOG"),
        )
    )


def _rebuild_is_consistent(db: Session, rebuild: RebuildAttempt) -> bool:
    """Require rebuild ownership and lineage to match its snapshot and execution run."""
    snapshot = (
        db.query(ModelSnapshot)
        .filter(ModelSnapshot.snapshot_id == rebuild.snapshot_id)
        .first()
    )
    run = db.query(ModelRun).filter(ModelRun.id == rebuild.execution_run_id).first()
    project = db.query(Project).filter(Project.project_id == rebuild.project_id).first()
    model = db.query(MLModel).filter(MLModel.model_id == rebuild.model_id).first()
    parents_match = bool(
        snapshot is not None
        and run is not None
        and project is not None
        and model is not None
        and snapshot.user_id == rebuild.user_id
        and snapshot.project_id == rebuild.project_id
        and snapshot.model_id == rebuild.model_id
        and project.user_id == rebuild.user_id
        and model.user_id == rebuild.user_id
        and model.project_id == rebuild.project_id
        and run.user_id == rebuild.user_id
        and run.project_id == rebuild.project_id
        and run.model_id == rebuild.model_id
        and run.experiment_id == snapshot.experiment_id
        and run.run_type == "REBUILD"
        and _snapshot_is_consistent(db, snapshot)
        and _run_is_consistent(db, run)
    )
    return parents_match and _artifact_is_consistent(
        db, rebuild.logs_artifact_id, user_id=rebuild.user_id,
        experiment_id=run.experiment_id if run is not None else -1,
        project_id=rebuild.project_id, model_id=rebuild.model_id, expected_kind="LOG",
    )

def list_projects(db: Session, professor_user: User) -> list[Project]:
    user_ids = _related_user_ids(db, professor_user)
    return (
        db.query(Project)
        .filter(Project.user_id.in_(user_ids))
        .order_by(Project.updated_at.desc())
        .all()
    )


def list_models(db: Session, project_id: str, professor_user: User) -> list[MLModel]:
    user_ids = _related_user_ids(db, professor_user)
    project = _related_project(db, project_id, user_ids)
    return (
        db.query(MLModel)
        .filter(MLModel.project_id == project.project_id, MLModel.user_id == project.user_id)
        .order_by(MLModel.created_at.desc())
        .all()
    )


def list_experiments(db: Session, project_id: str, professor_user: User) -> list[Experiment]:
    user_ids = _related_user_ids(db, professor_user)
    project = _related_project(db, project_id, user_ids)
    experiments = (
        db.query(Experiment)
        .filter(Experiment.project_id == project.project_id, Experiment.user_id == project.user_id)
        .order_by(Experiment.updated_at.desc())
        .all()
    )
    return [
        experiment for experiment in experiments
        if db.query(MLModel).filter(
            MLModel.model_id == experiment.model_id,
            MLModel.project_id == experiment.project_id,
            MLModel.user_id == experiment.user_id,
        ).first() is not None
    ]


def list_runs(db: Session, experiment_id: int, professor_user: User) -> list[ModelRun]:
    user_ids = _related_user_ids(db, professor_user)
    experiment = _related_experiment(db, experiment_id, user_ids)
    runs = (
        db.query(ModelRun)
        .filter(ModelRun.experiment_id == experiment.id, ModelRun.user_id == experiment.user_id)
        .order_by(ModelRun.created_at.desc())
        .all()
    )
    return [run for run in runs if _run_is_consistent(db, run)]


def get_run(db: Session, run_id: int, professor_user: User) -> ModelRun:
    user_ids = _related_user_ids(db, professor_user)
    run = (
        db.query(ModelRun)
        .filter(ModelRun.id == run_id, ModelRun.user_id.in_(user_ids))
        .first()
    )
    if run is None or not _run_is_consistent(db, run):
        raise NotFoundError("Run not found")
    return run


def list_metrics(db: Session, run_id: int, professor_user: User) -> list[RunMetric]:
    run = get_run(db, run_id, professor_user)
    return (
        db.query(RunMetric)
        .filter(RunMetric.run_id == run.id, RunMetric.user_id == run.user_id)
        .order_by(RunMetric.step.asc(), RunMetric.recorded_at.asc())
        .all()
    )


def list_snapshots(db: Session, experiment_id: int, professor_user: User) -> list[ModelSnapshot]:
    user_ids = _related_user_ids(db, professor_user)
    experiment = _related_experiment(db, experiment_id, user_ids)
    snapshots = (
        db.query(ModelSnapshot)
        .filter(
            ModelSnapshot.experiment_id == experiment.id,
            ModelSnapshot.user_id == experiment.user_id,
        )
        .order_by(ModelSnapshot.created_at.desc())
        .all()
    )
    return [snapshot for snapshot in snapshots if _snapshot_is_consistent(db, snapshot)]


def get_snapshot(db: Session, snapshot_id: str, professor_user: User) -> ModelSnapshot:
    user_ids = _related_user_ids(db, professor_user)
    snapshot = (
        db.query(ModelSnapshot)
        .filter(
            ModelSnapshot.snapshot_id == snapshot_id,
            ModelSnapshot.user_id.in_(user_ids),
        )
        .first()
    )
    if snapshot is None or not _snapshot_is_consistent(db, snapshot):
        raise NotFoundError("Snapshot not found")
    return snapshot


def list_artifacts(db: Session, experiment_id: int, professor_user: User) -> list[Artifact]:
    user_ids = _related_user_ids(db, professor_user)
    experiment = _related_experiment(db, experiment_id, user_ids)
    artifacts = (
        db.query(Artifact)
        .filter(Artifact.experiment_id == experiment.id, Artifact.user_id == experiment.user_id)
        .order_by(Artifact.created_at.desc())
        .all()
    )
    return [
        artifact for artifact in artifacts
        if artifact.project_id == experiment.project_id
        and artifact.model_id == experiment.model_id
        and db.query(Project).filter(
            Project.project_id == artifact.project_id, Project.user_id == artifact.user_id
        ).first() is not None
        and db.query(MLModel).filter(
            MLModel.model_id == artifact.model_id,
            MLModel.project_id == artifact.project_id,
            MLModel.user_id == artifact.user_id,
        ).first() is not None
    ]


def list_rebuilds(db: Session, snapshot_id: str, professor_user: User) -> list[RebuildAttempt]:
    snapshot = get_snapshot(db, snapshot_id, professor_user)
    rebuilds = (
        db.query(RebuildAttempt)
        .filter(
            RebuildAttempt.snapshot_id == snapshot.snapshot_id,
            RebuildAttempt.user_id == snapshot.user_id,
        )
        .order_by(RebuildAttempt.created_at.desc())
        .all()
    )
    return [rebuild for rebuild in rebuilds if _rebuild_is_consistent(db, rebuild)]


def get_rebuild(db: Session, rebuild_id: int, professor_user: User) -> RebuildAttempt:
    user_ids = _related_user_ids(db, professor_user)
    rebuild = (
        db.query(RebuildAttempt)
        .filter(RebuildAttempt.id == rebuild_id, RebuildAttempt.user_id.in_(user_ids))
        .first()
    )
    if rebuild is None or not _rebuild_is_consistent(db, rebuild):
        raise NotFoundError("Rebuild attempt not found")
    return rebuild
