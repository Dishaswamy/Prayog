from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from database import get_db
from logic.scheduler.claim import utc_now
from logic.scheduler.metrics import collect, render

router = APIRouter(tags=["metrics"])


# Scheduler metrics for Prometheus (NFR-GPU-051). Counts only, no job contents.
@router.get("/metrics", response_class=PlainTextResponse)
def metrics(db: Session = Depends(get_db)):
    return PlainTextResponse(render(collect(db, utc_now())), media_type="text/plain; version=0.0.4")
