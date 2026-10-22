"""Project versions and GPU runs under /mlflow: the folder a user uploaded, which version is
current, and the numbered runs made from it (A run 1, A run 2, B run 1, ...)."""
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from database import get_db
from logic import gpu_runs, project_versions, staging
from logic.dependencies import get_current_user
from logic.mlflow_release import displayed_status
from logic.project_versions import list_versions, register_version, set_current
from logic.scheduler.config import SchedulerConfig
from logic.scheduler.passes import Trigger, run_pass
from logic.scheduler.requests import format_walltime
from mlflow_models import ModelRun, RunDatasetFile, StagedFile
from models import User
from routers.scheduler.jobs import get_pass_sessions, get_scheduler_config
from schemas.mlflow_runs import (
    CheckpointOut, CurrentVersionSet, DatasetFileOut, GpuRunOut, ProjectStatusOut, RunSubmit, StagedOut,
    RebuildComparison, StagingCheck, StagingClearOut, StagingMissing, VersionOut, VersionRegister, VersionRegisterOut,
)

router = APIRouter(prefix="/mlflow", tags=["ML Flow runs"])


def _info(db: Session, user: User, project_id: str, version_id: int):
    return next(i for i in list_versions(db, user, project_id) if i.version.id == version_id)


@router.post("/projects/{project_id}/versions", response_model=VersionRegisterOut, status_code=status.HTTP_201_CREATED)
def add_version(project_id: str, body: VersionRegister, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    version, created = register_version(db, user, project_id, body.manifest_artifact_id)
    return VersionRegisterOut(**VersionOut.of(_info(db, user, project_id, version.id)).model_dump(), created=created)


@router.get("/projects/{project_id}/versions", response_model=list[VersionOut])
def versions(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return [VersionOut.of(info) for info in list_versions(db, user, project_id)]


@router.put("/projects/{project_id}/current", response_model=VersionOut)
def checkout(project_id: str, body: CurrentVersionSet, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    version = set_current(db, user, project_id, body.version)
    return VersionOut.of(_info(db, user, project_id, version.id))


# ── temporary dataset staging ─────────────────────────────────────────

@router.post("/staging/missing", response_model=StagingMissing)
def staging_missing(body: StagingCheck, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Which of these files must be uploaded? Files already staged are skipped, so a repeat run sends nothing."""
    return StagingMissing(missing=staging.missing_files(db, user.id, [(f.blake3, f.size_bytes) for f in body.files]))


@router.put(
    "/staging/files/{blake3}",
    response_model=StagedOut,
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def stage_file(blake3: str, size: int, request: Request, response: Response, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Upload one dataset file's bytes. The server recomputes the BLAKE3 digest and refuses a mismatch."""
    row, created = await staging.store_staged_file(db, user, blake3, size, request)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return StagedOut(blake3=row.blake3, size_bytes=row.size_bytes, created=created)


@router.delete("/projects/{project_id}/staged-data", response_model=StagingClearOut)
def clear_staged_data(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Delete now the temporary data this project's runs used (what is still needed by a running job is kept)."""
    project_versions._owned_project(db, project_id, user)
    return StagingClearOut(**staging.clear_project_staging(db, user.id, project_id))


# ── GPU runs ──────────────────────────────────────────────────────────

def run_out(view: gpu_runs.RunView, created: bool = True) -> GpuRunOut:
    run, job = view.run, view.job
    return GpuRunOut(
        run_id=run.id, label=view.label, rebuild_of=view.rebuild_of, version=view.version, run_number=run.run_number, job_id=job.id,
        status=displayed_status(run, job.state), job_state=job.state, queue_reason=job.state_reason if job.state == "QUEUED" else None,
        gpus=job.gpus, walltime=format_walltime(job.walltime_minutes), hyperparameters=run.hyperparameters,
        dataset=[DatasetFileOut(path=f.path, size_bytes=f.size_bytes, blake3=f.blake3, staged=staged)
                 for f, staged in view.dataset],
        checkpoints=[CheckpointOut(step=c.step, is_best=c.is_best, artifact_id=c.artifact_id, size_bytes=size)
                     for c, size in view.checkpoints],
        metrics=view.last_metrics, logs_artifact_id=run.logs_artifact_id, snapshot_id=view.snapshot_id,
        error_summary=run.error_summary, created_at=run.created_at, started_at=run.started_at,
        finished_at=run.finished_at, created=created,
    )


@router.post("/projects/{project_id}/runs", response_model=GpuRunOut, status_code=status.HTTP_201_CREATED)
def submit_run(
    project_id: str,
    body: RunSubmit,
    response: Response,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    """Queue a run of a version (default: the current one) on the GPUs. The dataset's files must be staged."""
    run, created = gpu_runs.submit_gpu_run(
        db, user, project_id, body.version, body.gpus, body.walltime, body.client_request_id,
        hyperparameters=body.hyperparameters, entrypoint=body.entrypoint, cfg=cfg, rebuild_of=body.rebuild_of,
        dataset=[gpu_runs.DatasetFile(f.path, f.size_bytes, f.blake3) for f in body.dataset],
    )
    if created:
        background.add_task(run_pass, pass_sessions, Trigger.SUBMIT, cfg=cfg)          # FR-GPU-121
    else:
        response.status_code = status.HTTP_200_OK                                       # a retry (FR-GPU-107)
    return run_out(gpu_runs._view(db, run), created)


@router.get("/projects/{project_id}/runs", response_model=list[GpuRunOut])
def project_runs(project_id: str, version: str | None = None, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    return [run_out(view) for view in gpu_runs.list_runs(db, user, project_id, version)]


@router.get("/projects/{project_id}/runs/{version}/{number}", response_model=GpuRunOut)
def project_run(project_id: str, version: str, number: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    return run_out(gpu_runs.get_run(db, user, project_id, version, number))


@router.get("/projects/{project_id}/runs/{version}/{number}/compare", response_model=RebuildComparison)
def compare_run(project_id: str, version: str, number: int, tolerance: float = 0.0, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Compare a rebuild with the run it reproduces: its metrics within a tolerance, and the weights byte for byte."""
    return gpu_runs.compare_rebuild(db, user, project_id, version, number, tolerance)


@router.get("/projects/{project_id}/status", response_model=ProjectStatusOut)
def project_status(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    project = project_versions._owned_project(db, project_id, user)
    infos = project_versions.list_versions(db, user, project_id)
    runs = gpu_runs.list_runs(db, user, project_id)
    held = db.query(func.count(StagedFile.id), func.coalesce(func.sum(StagedFile.size_bytes), 0)).filter(
        StagedFile.id.in_(db.query(RunDatasetFile.staged_file_id).join(ModelRun, ModelRun.id == RunDatasetFile.run_id)
                          .filter(ModelRun.project_id == project_id, RunDatasetFile.staged_file_id.is_not(None))),
        StagedFile.deleted_at.is_(None)).one()
    return ProjectStatusOut(
        project_id=project.project_id, name=project.name,
        current_version=next((i.label for i in infos if i.is_current), None),
        head_version=infos[0].label if infos else None, versions=len(infos), runs=len(runs),
        latest_run=run_out(runs[0]) if runs else None, staged_files=held[0], staged_bytes=int(held[1]),
    )
