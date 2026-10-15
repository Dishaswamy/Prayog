from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from database import get_db
from logic.dependencies import get_current_user, require_roles
from logic.mlflow_handover import (
    approve_handover,
    cancel_handover,
    get_handover,
    push_project,
    reject_handover,
)
from models import User
from schemas.handover import HandoverOut, HandoverPush

router = APIRouter(prefix="/mlflow", tags=["ML Flow handover"])


@router.post("/projects/{project_id}/handover", response_model=HandoverOut, status_code=status.HTTP_201_CREATED)
def push(
    project_id: str,
    body: HandoverPush,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("STUDENT", "RESEARCHER")),
):
    return push_project(db, project_id, user, body.professor_id)


@router.get("/handovers/{handover_id}", response_model=HandoverOut)
def details(
    handover_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return get_handover(db, handover_id, user)


@router.post("/handovers/{handover_id}/approve", response_model=HandoverOut)
def approve(
    handover_id: str,
    db: Session = Depends(get_db),
    professor: User = Depends(require_roles("PROFESSOR")),
):
    return approve_handover(db, handover_id, professor)


@router.post("/handovers/{handover_id}/reject", response_model=HandoverOut)
def reject(
    handover_id: str,
    db: Session = Depends(get_db),
    professor: User = Depends(require_roles("PROFESSOR")),
):
    return reject_handover(db, handover_id, professor)


@router.post("/handovers/{handover_id}/cancel", response_model=HandoverOut)
def cancel(
    handover_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("STUDENT", "RESEARCHER")),
):
    return cancel_handover(db, handover_id, user)
