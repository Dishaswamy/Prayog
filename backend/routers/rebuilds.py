from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy.orm import Session

from database import SessionLocal, get_db
from logic.dependencies import get_current_user
from logic.rebuild import AttemptStatus, get_attempt, list_attempts, start_rebuild
from logic.scheduler.passes import Trigger, run_pass
from models import User
from routers.scheduler.jobs import get_scheduler_config
from schemas.rebuild import RebuildOut, RebuildStart

router = APIRouter(prefix="/rebuilds", tags=["rebuilds"])


def _out(s: AttemptStatus) -> RebuildOut:
    return RebuildOut(
        attempt_id=s.attempt.id,
        snapshot_hash=s.attempt.snapshot_hash,
        job_id=s.job.id,
        job_state=s.job.state,
        state=s.state,
        created_at=s.attempt.created_at,
    )


# Rebuild a recorded snapshot: an ordinary job under every scheduling rule (FR-INT-01).
# A snapshot none of your jobs ran is REBUILD_REJECTED (404, no attempt created).
@router.post("", response_model=RebuildOut, status_code=status.HTTP_201_CREATED)
def start(
    body: RebuildStart,
    response: Response,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cfg = get_scheduler_config()
    attempt, created = start_rebuild(db, current_user.id, cfg=cfg, **body.model_dump())
    if created:
        background.add_task(run_pass, SessionLocal, Trigger.SUBMIT, cfg=cfg)
    else:
        response.status_code = status.HTTP_200_OK          # a retry returns the same attempt
    return _out(attempt)


@router.get("", response_model=list[RebuildOut])
def mine(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return [_out(a) for a in list_attempts(db, current_user.id)]


@router.get("/{attempt_id}", response_model=RebuildOut)
def one(attempt_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return _out(get_attempt(db, current_user.id, attempt_id))
