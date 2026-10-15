import os

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from logic.dependencies import require_roles
from mlflow_models import (
    Artifact,
    Experiment,
    MLFlowOutboxEvent,
    MLModel,
    ModelRun,
    ModelSnapshot,
    Project,
    RebuildAttempt,
    RunMetric,
)
from models import User

router = APIRouter(prefix="/admin/mlflow", tags=["ML Flow administration"])


def _count(db: Session, model) -> int:
    return db.query(func.count()).select_from(model).scalar() or 0


def _counts_by(db: Session, model, field) -> dict[str, int]:
    rows = db.query(field, func.count()).group_by(field).all()
    return {str(key): count for key, count in rows}


@router.get("/overview")
def mlflow_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("ADMIN")),
):
    """Return aggregate operational counts only; never expose user or artifact content."""
    logical_artifact_bytes = (
        db.query(func.coalesce(func.sum(Artifact.size_bytes), 0)).scalar() or 0
    )
    return {
        "availability": {
            "database": "available",
            # Configuration indicator only; this does not probe object-store connectivity.
            "artifact_storage": "configured",
            "artifact_storage_backend": (
                "s3" if os.getenv("PRAYOG_ARTIFACT_STORAGE", "local").lower() == "s3" else "local"
            ),
        },
        "volume": {
            "projects": _count(db, Project),
            "models": _count(db, MLModel),
            "experiments": _count(db, Experiment),
            "runs": _count(db, ModelRun),
            "runs_by_status": _counts_by(db, ModelRun, ModelRun.status),
            "snapshots": _count(db, ModelSnapshot),
            "artifacts": _count(db, Artifact),
            # Logical metadata total; deduplicated physical object bytes may be lower.
            "logical_artifact_bytes": int(logical_artifact_bytes),
            "metrics": _count(db, RunMetric),
            "rebuilds": _count(db, RebuildAttempt),
            "rebuilds_by_state": _counts_by(db, RebuildAttempt, RebuildAttempt.state),
            "outbox_events": _count(db, MLFlowOutboxEvent),
            "outbox_events_by_status": _counts_by(db, MLFlowOutboxEvent, MLFlowOutboxEvent.status),
        },
    }
