from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy.orm import Session, sessionmaker

from database import SessionLocal, get_db
from logic.dependencies import get_current_user
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import JobState
from logic.scheduler.passes import Trigger, run_pass
from logic.scheduler.requests import format_walltime, job_status, submit_job
from models import User
from schemas.scheduler.jobs import JobOut, JobSubmit, SubmissionOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


# Overridable in tests: the configuration, and the sessions the triggered pass opens.
def get_scheduler_config() -> SchedulerConfig:
    return DEFAULT_CONFIG


def get_pass_sessions() -> sessionmaker:
    return SessionLocal


# Submit a GPU request. Answered on the spot; the scheduling pass runs after the response is sent.
@router.post("", response_model=SubmissionOut, status_code=status.HTTP_201_CREATED)
def submit(
    body: JobSubmit,
    response: Response,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    result = submit_job(db, current_user.id, **body.model_dump(), cfg=cfg)
    job_id = result.job.id
    if result.created:
        background.add_task(run_pass, pass_sessions, Trigger.SUBMIT, cfg=cfg)        # FR-GPU-121
        return SubmissionOut(
            request_id=job_id,
            status=JobState.IN_PROCESS,
            message=f"Request submitted. Request ID: {job_id}. Status: IN PROCESS",
        )
    response.status_code = status.HTTP_200_OK                                       # a retry (FR-GPU-107)
    return SubmissionOut(
        request_id=job_id,
        status=result.job.state,
        message=f"Request already submitted. Request ID: {job_id}. Status: {result.job.state}",
    )


# The owner's view of one job: state, queue reason, and where it is placed.
@router.get("/{job_id}", response_model=JobOut)
def one_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    s = job_status(db, current_user.id, job_id)
    return JobOut(
        id=s.job.id,
        state=s.job.state,
        queue_reason=s.queue_reason,
        gpus=s.job.gpus,
        walltime=format_walltime(s.job.walltime_minutes),
        submitted_at=s.job.submitted_at,
        node_id=s.node_id,
        node_name=s.node_name,
        unit_indexes=list(s.unit_indexes),
    )
