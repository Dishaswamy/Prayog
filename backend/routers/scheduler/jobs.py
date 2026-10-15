from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy.orm import Session, sessionmaker

from database import SessionLocal, get_db
from logic.dependencies import get_current_user
from logic.scheduler.cancel import cancel_job
from logic.scheduler.config import SchedulerConfig, load_config
from logic.scheduler.domain import JobState
from logic.scheduler.passes import Trigger, run_pass
from logic.scheduler.requests import edit_job, format_walltime, job_status, quota_status, submit_job
from models import User
from schemas.scheduler.jobs import CancelOut, JobEdit, JobOut, JobSubmit, MemberUsage, QuotaOut, SubmissionOut

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Read from PRAYOG_SCHED_* variables and checked once, when the app starts: an invalid
# configuration stops startup with the violated invariant named (NFR-GPU-062).
ACTIVE_CONFIG = load_config()


# Overridable in tests: the configuration, and the sessions the triggered pass opens.
def get_scheduler_config() -> SchedulerConfig:
    return ACTIVE_CONFIG


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


# A course account's sponsorship cap: total, used, committed, left, and per member.
# Declared before /{job_id} so "quota" is not read as a job ID.
@router.get("/quota", response_model=QuotaOut)
def my_quota(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = quota_status(db, current_user.id)
    return QuotaOut(
        cap_gpu_hours=q.cap,
        consumed_gpu_hours=q.consumed,
        committed_gpu_hours=q.committed,
        remaining_gpu_hours=q.remaining,
        members=[MemberUsage(user_id=uid, consumed_gpu_hours=hours) for uid, hours in q.members],
    )


# The owner's view of one job: state, queue reason and until when, and where it is placed.
@router.get("/{job_id}", response_model=JobOut)
def one_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
):
    return _job_out(db, current_user.id, job_id, cfg)


# Edit a QUEUED job's GPUs, walltime or snapshot. Growing the request moves it to the back of the queue.
@router.patch("/{job_id}", response_model=JobOut)
def edit(
    job_id: int,
    body: JobEdit,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    edit_job(db, current_user.id, job_id, **body.model_dump(exclude_unset=True), cfg=cfg)
    background.add_task(run_pass, pass_sessions, Trigger.EDIT, cfg=cfg)              # FR-GPU-121
    return _job_out(db, current_user.id, job_id, cfg)


# Cancel your own job. A running job stops first; its GPUs are freed when the node confirms.
@router.post("/{job_id}/cancel", response_model=CancelOut)
def cancel(
    job_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    result = cancel_job(db, job_id, current_user.id, cfg=cfg)
    background.add_task(run_pass, pass_sessions, Trigger.CANCEL, cfg=cfg)            # FR-GPU-121
    return CancelOut.from_result(result)


def _job_out(db: Session, user_id: int, job_id: int, cfg: SchedulerConfig) -> JobOut:
    s = job_status(db, user_id, job_id, cfg=cfg)
    return JobOut(
        id=s.job.id,
        state=s.job.state,
        queue_reason=s.queue_reason,
        p0_free_at=s.p0_free_at,
        gpus=s.job.gpus,
        walltime=format_walltime(s.job.walltime_minutes),
        submitted_at=s.job.submitted_at,
        node_id=s.node_id,
        node_name=s.node_name,
        unit_indexes=list(s.unit_indexes),
        started_at=s.run.started_at if s.run else None,
        walltime_ends_at=s.walltime_ends_at,
        stop_reason=s.stop_reason,
        planned_stop_at=s.planned_stop_at,
        planned_stop_reason=s.planned_stop_reason,
        ended_at=s.run.ended_at if s.run else None,
        end_state=s.run.end_state if s.run else None,
        exit_code=s.run.exit_code if s.run else None,
        cancel_reason=s.job.cancel_reason,
        dispatch_attempts=s.job.dispatch_attempts,
    )
