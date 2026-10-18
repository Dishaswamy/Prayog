from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from logic.auth import create_user_record, generate_random_password, normalize_email
from logic.errors import ConflictError, NotFoundError
from models import Lab, Professor


def create_professor(
    db: Session,
    name: str,
    email: str,
    department: str,
    password: str | None = None,
    lab_id: int | None = None,
) -> Professor:
    """
    Create the PROFESSOR user and the Professor profile in ONE transaction.

    With no password given, one is generated and left on the returned object as
    `temporary_password`. It is shown to the admin once, to pass to the professor,
    who can log in with it directly and change it later with the password reset.
    """
    email = normalize_email(email)
    generated = None if password else generate_random_password()

    if lab_id is not None and db.get(Lab, lab_id) is None:
        raise NotFoundError("Lab not found")

    if db.query(Professor).filter(Professor.email == email).first():
        raise ConflictError("A professor with this email already exists")

    user = create_user_record(db, email, password or generated, "PROFESSOR")   # no commit yet

    professor = Professor(
        user_id=user.id,
        name=name,
        email=email,          # same email as the login account
        department=department,
        lab_id=lab_id,
    )
    db.add(professor)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("A user or professor with this email already exists")

    db.refresh(professor)
    professor.temporary_password = generated     # not a column: only for this response
    return professor


def list_professors(db: Session) -> list[Professor]:
    return db.query(Professor).order_by(Professor.name).all()
