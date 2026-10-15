from logic.errors import ForbiddenError
from mlflow_models import MLFlowLedgerEntry, ModelRun, ModelSnapshot
from models import Professor, Student, User


def list_professor_ledger(db, user: User) -> list[dict]:
    if user.role != "PROFESSOR":
        raise ForbiddenError("Professor access is required")

    professor = db.query(Professor).filter(Professor.user_id == user.id).first()
    if professor is None:
        raise ForbiddenError("Professor access is required")

    students = db.query(Student).filter(Student.professor_id == professor.id).all()
    if not students:
        return []

    student_by_user_id = {student.user_id: student for student in students}
    user_ids = list(student_by_user_id)
    runs = db.query(ModelRun).filter(ModelRun.user_id.in_(user_ids)).all()
    if not runs:
        return []

    run_by_id = {run.id: run for run in runs}
    run_ids = list(run_by_id)
    entries = (
        db.query(MLFlowLedgerEntry)
        .filter(MLFlowLedgerEntry.payload["run_id"].as_integer().in_(run_ids))
        .order_by(MLFlowLedgerEntry.consumed_at.desc())
        .all()
    )
    if not entries:
        return []

    snapshots = (
        db.query(ModelSnapshot)
        .filter(
            ModelSnapshot.run_id.in_(run_ids),
            ModelSnapshot.user_id.in_(user_ids),
        )
        .all()
    )
    snapshot_by_run_id = {snapshot.run_id: snapshot for snapshot in snapshots}

    result = []
    for entry in entries:
        payload = entry.payload
        run_id = payload.get("run_id")
        run = run_by_id.get(run_id)
        snapshot = snapshot_by_run_id.get(run_id)
        if (
            run is None
            or snapshot is None
            or run.user_id not in student_by_user_id
            or snapshot.user_id != run.user_id
            or payload.get("state") != "RECORDED"
            or payload.get("snapshot_id") != snapshot.snapshot_id
            or snapshot.state != "RECORDED"
        ):
            continue

        student = student_by_user_id[run.user_id]
        result.append({
            "student_name": student.name,
            "student_roll_number": student.roll_number,
            "run_id": run.id,
            "snapshot_id": snapshot.snapshot_id,
            "experiment_id": snapshot.experiment_id,
            "project_id": snapshot.project_id,
            "model_id": snapshot.model_id,
            "state": snapshot.state,
            "code_sha256": snapshot.code_sha256,
            "sealed_at": entry.consumed_at,
        })
    return result
