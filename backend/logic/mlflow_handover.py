from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from logic.artifacts import get_verified_artifact
from logic.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from mlflow_models import (
    Artifact,
    Experiment,
    MLModel,
    ModelRun,
    ModelSnapshot,
    Project,
    ProjectHandover,
    RebuildAttempt,
    RunMetric,
)
from models import Professor, Researcher, Student, User

PENDING = "HANDOVER_PENDING"
TERMINAL = {"HANDOVER_APPROVED", "HANDOVER_REJECTED", "HANDOVER_CANCELLED"}
ACTIVE_RUN_STATES = {"CREATED", "QUEUED", "RUNNING"}


def _now():
    return datetime.utcnow()


def _project_for_owner(db: Session, project_id: str, owner_id: int, lock: bool = False):
    query = db.query(Project).filter(Project.project_id == project_id, Project.user_id == owner_id)
    if lock:
        query = query.with_for_update()
    project = query.first()
    if project is None:
        raise NotFoundError("Project not found")
    return project


def _target_professor(db: Session, owner_id: int, professor_id: int) -> Professor:
    professor = db.query(Professor).filter(Professor.id == professor_id).first()
    if professor is None:
        raise NotFoundError("Target Professor not found")
    target_user = db.query(User).filter(User.id == professor.user_id).first()
    if target_user is None or target_user.role != "PROFESSOR":
        raise BadRequestError("Target is not an authorized Professor account")
    student = db.query(Student).filter(
        Student.user_id == owner_id, Student.professor_id == professor.id
    ).first()
    researcher = db.query(Researcher).filter(
        Researcher.user_id == owner_id, Researcher.professor_id == professor.id
    ).first()
    if student is None and researcher is None:
        raise ForbiddenError("Target Professor is not the owner's supervising Professor")
    return professor


def push_project(db: Session, project_id: str, owner: User, professor_id: int) -> ProjectHandover:
    project = _project_for_owner(db, project_id, owner.id, lock=True)
    professor = _target_professor(db, owner.id, professor_id)
    existing = db.query(ProjectHandover).filter(
        ProjectHandover.project_id == project.project_id,
        ProjectHandover.status == PENDING,
    ).with_for_update().first()
    if existing is not None:
        raise ConflictError("A handover is already pending for this project")

    artifacts = db.query(Artifact).filter(
        Artifact.project_id == project.project_id,
        Artifact.user_id == owner.id,
    ).order_by(Artifact.id).all()
    if not artifacts:
        raise BadRequestError("Project has no owned artifacts to hand over")

    inventory = [
        {"artifact_id": item.id, "kind": item.kind, "sha256": item.sha256, "size_bytes": item.size_bytes}
        for item in artifacts
    ]
    handover = ProjectHandover(
        handover_id=str(uuid4()),
        project_id=project.project_id,
        source_owner_id=owner.id,
        target_professor_id=professor.id,
        status=PENDING,
        artifact_inventory=inventory,
        failed_artifacts=[],
    )
    db.add(handover)
    db.commit()
    db.refresh(handover)
    return handover


def _handover_for_decision(db: Session, handover_id: str, professor: User, lock: bool = True):
    profile = db.query(Professor).filter(Professor.user_id == professor.id).first()
    if profile is None:
        raise NotFoundError("Professor profile not found")
    query = db.query(ProjectHandover).filter(
        ProjectHandover.handover_id == handover_id,
        ProjectHandover.target_professor_id == profile.id,
    )
    if lock:
        query = query.with_for_update()
    handover = query.first()
    if handover is None:
        raise NotFoundError("Handover request not found")
    return handover, profile


def _verify_inventory(db: Session, handover: ProjectHandover) -> list[str]:
    failed = []
    expected = {entry["artifact_id"]: entry for entry in handover.artifact_inventory}
    current = db.query(Artifact).filter(
        Artifact.project_id == handover.project_id,
        Artifact.user_id == handover.source_owner_id,
    ).order_by(Artifact.id).all()
    current_by_id = {artifact.id: artifact for artifact in current}

    # The Professor approves a specific inventory. Any artifact added, removed, or
    # changed after Push requires a fresh handover rather than silently expanding it.
    failed.extend(sorted(set(current_by_id) - set(expected)))
    failed.extend(sorted(set(expected) - set(current_by_id)))
    for artifact_id in sorted(set(current_by_id) & set(expected)):
        artifact = current_by_id[artifact_id]
        entry = expected[artifact_id]
        if (
            artifact.kind != entry["kind"]
            or artifact.sha256 != entry["sha256"]
            or artifact.size_bytes != entry["size_bytes"]
        ):
            failed.append(artifact_id)
            continue
        try:
            get_verified_artifact(
                db, artifact.id, handover.source_owner_id, artifact.experiment_id, artifact.kind
            )
        except Exception:
            failed.append(artifact.id)
    return sorted(set(failed))


def approve_handover(db: Session, handover_id: str, professor: User) -> ProjectHandover:
    handover, profile = _handover_for_decision(db, handover_id, professor)
    if handover.status != PENDING:
        raise ConflictError("Only HANDOVER_PENDING requests can be approved")
    project = db.query(Project).filter(Project.project_id == handover.project_id).with_for_update().first()
    if project is None or project.user_id != handover.source_owner_id:
        raise ConflictError("Project ownership changed while the handover was pending")

    failed = _verify_inventory(db, handover)
    if failed:
        handover.failed_artifacts = failed
        db.commit()
        db.refresh(handover)
        raise ConflictError(f"Artifact verification failed for {len(failed)} artifact(s); ownership was not transferred")

    owner_id = handover.source_owner_id
    target_id = profile.user_id
    active_runs = db.query(ModelRun).filter(
        ModelRun.project_id == project.project_id,
        ModelRun.status.in_(ACTIVE_RUN_STATES),
    ).all()
    active_run_ids = {run.id for run in active_runs}
    active_rebuilds = db.query(RebuildAttempt).filter(
        RebuildAttempt.project_id == project.project_id,
        RebuildAttempt.state == "REBUILD_INITIATED",
    ).all()
    active_rebuild_ids = {attempt.id for attempt in active_rebuilds}
    protected_artifact_ids = set()
    for run in active_runs:
        protected_artifact_ids.update(
            artifact_id for artifact_id in (run.code_artifact_id, run.weights_artifact_id, run.logs_artifact_id)
            if artifact_id
        )
    for attempt in active_rebuilds:
        if attempt.logs_artifact_id:
            protected_artifact_ids.add(attempt.logs_artifact_id)
        execution = db.query(ModelRun).filter(ModelRun.id == attempt.execution_run_id).first()
        if execution is not None:
            protected_artifact_ids.update(
                artifact_id for artifact_id in (execution.code_artifact_id, execution.weights_artifact_id, execution.logs_artifact_id)
                if artifact_id
            )

    # Ownership and child metadata move in one database transaction. Running work
    # and its artifacts retain the original actor ID so workers can finish in context.
    project.user_id = target_id
    db.query(MLModel).filter(
        MLModel.project_id == project.project_id, MLModel.user_id == owner_id
    ).update({MLModel.user_id: target_id}, synchronize_session=False)
    db.query(Experiment).filter(
        Experiment.project_id == project.project_id, Experiment.user_id == owner_id
    ).update({Experiment.user_id: target_id}, synchronize_session=False)
    db.query(ModelSnapshot).filter(
        ModelSnapshot.project_id == project.project_id, ModelSnapshot.user_id == owner_id
    ).update({ModelSnapshot.user_id: target_id}, synchronize_session=False)
    db.query(ModelRun).filter(
        ModelRun.project_id == project.project_id,
        ModelRun.user_id == owner_id,
        ~ModelRun.id.in_(active_run_ids) if active_run_ids else True,
    ).update({ModelRun.user_id: target_id}, synchronize_session=False)
    db.query(RunMetric).filter(
        RunMetric.user_id == owner_id,
        RunMetric.run_id.in_(
            db.query(ModelRun.id).filter(ModelRun.project_id == project.project_id)
        ),
        ~RunMetric.run_id.in_(active_run_ids) if active_run_ids else True,
    ).update({RunMetric.user_id: target_id}, synchronize_session=False)
    db.query(RebuildAttempt).filter(
        RebuildAttempt.project_id == project.project_id,
        RebuildAttempt.user_id == owner_id,
        RebuildAttempt.state != "REBUILD_INITIATED",
    ).update({RebuildAttempt.user_id: target_id}, synchronize_session=False)
    db.query(Artifact).filter(
        Artifact.project_id == project.project_id,
        Artifact.user_id == owner_id,
        ~Artifact.id.in_(protected_artifact_ids) if protected_artifact_ids else True,
    ).update({Artifact.user_id: target_id}, synchronize_session=False)

    handover.status = "HANDOVER_APPROVED"
    handover.decided_at = _now()
    db.commit()
    db.refresh(handover)
    return handover


def reject_handover(db: Session, handover_id: str, professor: User) -> ProjectHandover:
    handover, _ = _handover_for_decision(db, handover_id, professor)
    if handover.status != PENDING:
        raise ConflictError("Only HANDOVER_PENDING requests can be rejected")
    handover.status = "HANDOVER_REJECTED"
    handover.decided_at = _now()
    db.commit()
    db.refresh(handover)
    return handover


def cancel_handover(db: Session, handover_id: str, owner: User) -> ProjectHandover:
    handover = db.query(ProjectHandover).filter(
        ProjectHandover.handover_id == handover_id,
        ProjectHandover.source_owner_id == owner.id,
    ).with_for_update().first()
    if handover is None:
        raise NotFoundError("Handover request not found")
    if handover.status != PENDING:
        raise ConflictError("Only HANDOVER_PENDING requests can be cancelled")
    handover.status = "HANDOVER_CANCELLED"
    handover.decided_at = _now()
    db.commit()
    db.refresh(handover)
    return handover


def get_handover(db: Session, handover_id: str, user: User) -> ProjectHandover:
    handover = db.query(ProjectHandover).filter(
        ProjectHandover.handover_id == handover_id
    ).first()
    if handover is None:
        raise NotFoundError("Handover request not found")
    if user.id == handover.source_owner_id:
        return handover
    if user.role == "PROFESSOR":
        profile = db.query(Professor).filter(Professor.user_id == user.id).first()
        if profile is not None and profile.id == handover.target_professor_id:
            return handover
    if user.role == "ADMIN":
        return handover
    raise ForbiddenError("You are not allowed to view this handover")
