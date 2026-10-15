"""Read-only Professor views of ML Flow resources."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from logic.dependencies import require_roles
from logic.mlflow_professor import (
    get_rebuild,
    get_run,
    get_snapshot,
    list_artifacts,
    list_experiments,
    list_metrics,
    list_models,
    list_projects,
    list_rebuilds,
    list_runs,
    list_snapshots,
)
from models import User
from schemas.mlflow import (
    ProfessorArtifactOut,
    ProfessorExperimentOut,
    ProfessorModelOut,
    ProfessorProjectOut,
    ProfessorMetricOut,
    ProfessorRebuildOut,
    ProfessorRunOut,
    ProfessorSnapshotOut,
)

router = APIRouter(
    prefix="/mlflow/professor",
    tags=["ML Flow - Professor read-only"],
    dependencies=[Depends(require_roles("PROFESSOR"))],
)


@router.get("/projects", response_model=list[ProfessorProjectOut])
def professor_projects(db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return list_projects(db, user)


@router.get("/projects/{project_id}/models", response_model=list[ProfessorModelOut])
def professor_models(project_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return list_models(db, project_id, user)


@router.get("/projects/{project_id}/experiments", response_model=list[ProfessorExperimentOut])
def professor_experiments(project_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return list_experiments(db, project_id, user)


@router.get("/experiments/{experiment_id}/runs", response_model=list[ProfessorRunOut])
def professor_runs(experiment_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return list_runs(db, experiment_id, user)


@router.get("/experiments/{experiment_id}/snapshots", response_model=list[ProfessorSnapshotOut])
def professor_snapshots(experiment_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return list_snapshots(db, experiment_id, user)


@router.get("/experiments/{experiment_id}/artifacts", response_model=list[ProfessorArtifactOut])
def professor_artifacts(experiment_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    # Inventory metadata only. Artifact bytes remain owner-only via the existing content endpoint.
    return list_artifacts(db, experiment_id, user)


@router.get("/runs/{run_id}", response_model=ProfessorRunOut)
def professor_run(run_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return get_run(db, run_id, user)


@router.get("/runs/{run_id}/metrics", response_model=list[ProfessorMetricOut])
def professor_metrics(run_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return list_metrics(db, run_id, user)


@router.get("/snapshots/{snapshot_id}", response_model=ProfessorSnapshotOut)
def professor_snapshot(snapshot_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return get_snapshot(db, snapshot_id, user)


@router.get("/snapshots/{snapshot_id}/rebuilds", response_model=list[ProfessorRebuildOut])
def professor_snapshot_rebuilds(snapshot_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return list_rebuilds(db, snapshot_id, user)


@router.get("/rebuilds/{rebuild_id}", response_model=ProfessorRebuildOut)
def professor_rebuild(rebuild_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("PROFESSOR"))):
    return get_rebuild(db, rebuild_id, user)
