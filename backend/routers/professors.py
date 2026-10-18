from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from database import get_db
from logic.admin import create_professor, list_professors
from logic.dependencies import get_current_user, require_roles
from models import User
from schemas.professor import ProfessorCreate, ProfessorCreatedOut, ProfessorOut

router = APIRouter(prefix="/professors", tags=["professors"])


# Any logged-in user: used to fill the "choose professor" dropdown on the request form
@router.get("", response_model=list[ProfessorOut])
def all_professors(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return list_professors(db)


# Admin only: create a professor account + profile
@router.post("", response_model=ProfessorCreatedOut, status_code=status.HTTP_201_CREATED)
def add_professor(
    body: ProfessorCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("ADMIN")),
):
    return create_professor(
        db=db,
        name=body.name,
        email=body.email,
        department=body.department,
        password=body.password,
        lab_id=body.lab_id,
    )
