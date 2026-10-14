from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from database import get_db
from logic.access_request import (
    create_access_request,
    decide_access_request,
    get_access_request,
    list_my_requests,
    list_requests_for_reviewer,
    revoke_access,
)
from logic.dependencies import get_current_user, require_roles
from models import User
from schemas.access_request import (
    AccessRequestApproval,
    AccessRequestCreate,
    AccessRequestOut,
)

router = APIRouter(prefix="/access-requests", tags=["access requests"])


# Student / researcher submits a request
@router.post("", response_model=AccessRequestOut, status_code=status.HTTP_201_CREATED)
def submit_request(
    body: AccessRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("STUDENT", "RESEARCHER")),
):
    return create_access_request(db=db, current_user=current_user, **body.model_dump())


# Applicant sees their own request(s)
@router.get("/me", response_model=list[AccessRequestOut])
def my_requests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return list_my_requests(db, current_user)


# Professor: requests addressed to them. Admin: all.   e.g. ?status=PENDING
@router.get("", response_model=list[AccessRequestOut])
def reviewer_requests(
    status: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("PROFESSOR", "ADMIN")),
):
    return list_requests_for_reviewer(db, current_user, status)


# One request (applicant / their professor / admin)
@router.get("/{request_id}", response_model=AccessRequestOut)
def one_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_access_request(db, request_id, current_user)


# Professor / admin approve or reject (can change while not locked)
@router.post("/{request_id}/decision", response_model=AccessRequestOut)
def decide(
    request_id: int,
    body: AccessRequestApproval,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("PROFESSOR", "ADMIN")),
):
    return decide_access_request(db, request_id, current_user, body.status)


# Admin removes approved access (separate action)
@router.post("/{request_id}/revoke", response_model=AccessRequestOut)
def revoke(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("ADMIN")),
):
    return revoke_access(db, request_id, current_user)
