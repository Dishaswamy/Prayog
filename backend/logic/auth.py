import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from logic.errors import BadRequestError, ConflictError, ForbiddenError
from models import User

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY is not configured")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

ALLOWED_DOMAIN = "@iitk.ac.in"
SELF_REGISTER_ROLES = {"STUDENT", "RESEARCHER"}

password_hash = PasswordHash.recommended()


def normalize_email(email: str) -> str:
    # "ABC@IITK.ac.in" and "abc@iitk.ac.in" must be the same account
    return email.strip().lower()


def create_user_record(db: Session, email: str, password: str, role: str) -> User:
    """
    Build and add a User WITHOUT committing, so callers can create
    related rows (e.g. a Professor profile) in the same transaction.
    """
    email = normalize_email(email)

    if not email.endswith(ALLOWED_DOMAIN):
        raise BadRequestError("Only IITK email addresses are allowed")

    user_id = email.split("@")[0]

    if db.query(User).filter(User.email == email).first():
        raise ConflictError("User with this email already exists")

    if db.query(User).filter(User.user_id == user_id).first():
        raise ConflictError("User ID already exists")

    user = User(
        user_id=user_id,
        email=email,
        password_hash=password_hash.hash(password),
        role=role,
    )
    db.add(user)
    db.flush()          # assigns user.id without committing
    return user


def register_user(db: Session, email: str, password: str, role: str) -> User:
    """Public self-registration (students / researchers only)."""
    role = role.upper()
    if role not in SELF_REGISTER_ROLES:
        raise ForbiddenError("Only students and researchers can self-register")

    user = create_user_record(db, email, password, role)
    try:
        db.commit()
    except IntegrityError:
        # Two registrations with the same email at the same moment
        db.rollback()
        raise ConflictError("User with this email already exists")

    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if not user:
        return None
    if not password_hash.verify(password, user.password_hash):
        return None
    return user


def create_access_token(user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),     # must be a string for PyJWT >= 2.10
        "jti": secrets.token_urlsafe(24),
        "role": user.role,       # informational only; the DB role is what we trust
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
