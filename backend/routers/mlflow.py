from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db
from logic.dependencies import get_current_user
from logic.errors import NotFoundError
from logic.artifacts import get_verified_artifact, store_artifact
from logic.diagnostic_links import consume_diagnostic_link, create_diagnostic_link
from logic.mlflow_professor_ledger import list_professor_ledger
from logic.mlflow import (
    _owned_experiment,
    create_experiment,
    create_model,
    create_project,
    create_rebuild,
    create_run,
    finish_run,
    get_rebuild,
    get_run,
    get_snapshot,
    list_experiments,
    list_models,
    list_projects,
    list_metrics,
    list_rebuilds,
    list_runs,
    list_snapshots,
    log_metric,
    log_metrics,
    start_run,
    update_rebuild,
)
from mlflow_models import Artifact
from models import User
from schemas.mlflow import (
    DiagnosticLinkCreate,
    DiagnosticLinkOut,
    ExperimentCreate,
    ModelCreate,
    ModelOut,
    ProjectCreate,
    ProjectOut,
    ArtifactOut,
    ExperimentOut,
    MetricBatchCreate,
    MetricCreate,
    MetricOut,
    RebuildCreate,
    RebuildOut,
    RebuildUpdate,
    RunCreate,
    RunFinish,
    RunOut,
    RunStart,
    SnapshotOut,
)

router = APIRouter(prefix="/mlflow", tags=["ML Flow"])


@router.get("/artifacts/{artifact_id}/content")
def download_artifact(artifact_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    artifact = db.query(Artifact).filter(
        Artifact.id == artifact_id,
        Artifact.user_id == user.id,
    ).first()
    if artifact is None:
        raise NotFoundError("Artifact not found")
    _, path = get_verified_artifact(db, artifact.id, user.id, artifact.experiment_id, artifact.kind)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"{artifact.id}.artifact",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/rebuilds/{rebuild_id}/diagnostic-link", response_model=DiagnosticLinkOut, status_code=status.HTTP_201_CREATED)
def create_rebuild_diagnostic_link(
    rebuild_id: int,
    body: DiagnosticLinkCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    token, expires_at = create_diagnostic_link(db, rebuild_id, user, getattr(user, "session_id", ""), body.ttl_seconds)
    return DiagnosticLinkOut(
        path=f"/mlflow/diagnostic-artifacts/{token}",
        expires_at=expires_at,
    )


@router.get("/diagnostic-artifacts/{token}", name="consume_diagnostic_link")
def download_diagnostic_artifact(token: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    artifact, path = consume_diagnostic_link(db, token, user, getattr(user, "session_id", ""))
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"{artifact.id}.diagnostic.log",
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/experiments/{experiment_id}/artifacts",
    response_model=ArtifactOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def upload_artifact(
    experiment_id: int,
    request: Request,
    kind: Literal["CODE", "WEIGHTS", "LOG"],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    experiment = _owned_experiment(db, experiment_id, user)
    return await store_artifact(db, user, experiment, kind, request)


@router.get("/experiments/{experiment_id}/artifacts", response_model=list[ArtifactOut])
def artifacts(experiment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _owned_experiment(db, experiment_id, user)
    return db.query(Artifact).filter(
        Artifact.experiment_id == experiment_id,
        Artifact.user_id == user.id,
    ).order_by(Artifact.created_at.desc()).all()


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def add_project(body: ProjectCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return create_project(db, user, body.name, body.description)


@router.get("/projects", response_model=list[ProjectOut])
def projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_projects(db, user)


@router.post("/projects/{project_id}/models", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
def add_model(project_id: str, body: ModelCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return create_model(db, project_id, user, body.name, body.description)


@router.get("/projects/{project_id}/models", response_model=list[ModelOut])
def models(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_models(db, project_id, user)

@router.post("/experiments", response_model=ExperimentOut, status_code=status.HTTP_201_CREATED)
def add_experiment(body: ExperimentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return create_experiment(db, user, body.project_id, body.model_id, body.name, body.description, body.tags)


@router.get("/experiments", response_model=list[ExperimentOut])
def experiments(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_experiments(db, user)


@router.post("/experiments/{experiment_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def add_run(experiment_id: int, body: RunCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return create_run(db, experiment_id, user, body.hyperparameters, body.environment)


@router.get("/runs/{run_id}", response_model=RunOut)
def run_details(run_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return get_run(db, run_id, user)


@router.get("/experiments/{experiment_id}/runs", response_model=list[RunOut])
def runs(experiment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_runs(db, experiment_id, user)


@router.get("/experiments/{experiment_id}/snapshots", response_model=list[SnapshotOut])
def snapshots(experiment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_snapshots(db, experiment_id, user)


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotOut)
def snapshot(snapshot_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return get_snapshot(db, snapshot_id, user)


@router.post("/runs/{run_id}/start", response_model=RunOut)
def begin_run(run_id: int, body: RunStart, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return start_run(
        db, run_id, user, body.environment,
        body.code_artifact_id, body.weights_artifact_id,
    )


@router.post("/runs/{run_id}/finish", response_model=RunOut)
def complete_run(run_id: int, body: RunFinish, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return finish_run(db, run_id, user, body.model_dump())


@router.post("/runs/{run_id}/metrics", response_model=MetricOut, status_code=status.HTTP_201_CREATED)
def add_metric(run_id: int, body: MetricCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return log_metric(db, run_id, user, body.name, body.value, body.step)


@router.post("/runs/{run_id}/metrics/batch", response_model=list[MetricOut], status_code=status.HTTP_201_CREATED)
def add_metric_batch(run_id: int, body: MetricBatchCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return log_metrics(db, run_id, user, [metric.model_dump() for metric in body.metrics])


@router.get("/runs/{run_id}/metrics", response_model=list[MetricOut])
def metrics(run_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_metrics(db, run_id, user)


@router.post("/snapshots/{snapshot_id}/rebuilds", response_model=RebuildOut, status_code=status.HTTP_201_CREATED)
def add_rebuild(snapshot_id: str, body: RebuildCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return create_rebuild(db, snapshot_id, user, body.environment)


@router.get("/snapshots/{snapshot_id}/rebuilds", response_model=list[RebuildOut])
def rebuilds(snapshot_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_rebuilds(db, snapshot_id, user)


@router.get("/rebuilds/{rebuild_id}", response_model=RebuildOut)
def rebuild_details(rebuild_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return get_rebuild(db, rebuild_id, user)


@router.patch("/rebuilds/{rebuild_id}", response_model=RebuildOut)
def change_rebuild(rebuild_id: int, body: RebuildUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return update_rebuild(db, rebuild_id, user, body.state, body.logs_artifact_id, body.weights_artifact_id, body.failure_summary)


@router.get("/professor/ledger")
def professor_ledger(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_professor_ledger(db, user)
