from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from logic.auth import create_user_record, normalize_email
from logic.errors import ConflictError
from models import Professor


def create_professor(
    db: Session,
    name: str,
    email: str,
    department: str,
    password: str,
) -> Professor:
    """Create the PROFESSOR user and the Professor profile in ONE transaction."""
    email = normalize_email(email)

    if db.query(Professor).filter(Professor.email == email).first():
        raise ConflictError("A professor with this email already exists")

    user = create_user_record(db, email, password, "PROFESSOR")   # no commit yet

    professor = Professor(
        user_id=user.id,
        name=name,
        email=email,          # same email as the login account
        department=department,
    )
    db.add(professor)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("A user or professor with this email already exists")

    db.refresh(professor)
    return professor


def list_professors(db: Session) -> list[Professor]:
    return db.query(Professor).order_by(Professor.name).all()
