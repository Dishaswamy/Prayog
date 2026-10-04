import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import jwt

from sqlalchemy.orm import Session
from pwdlib import PasswordHash

from models import User
from schemas.auth import UserRole

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY is not configured")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60


password_hash = PasswordHash.recommended()


def register_user(
    db: Session,
    email: str,
    password: str,
    role: UserRole,
):
    # 1. IITK email validation
    if not email.endswith("@iitk.ac.in"):
        raise ValueError("Only IITK email addresses are allowed")

    # 2. Extract user_id
    user_id = email.split("@")[0]

    # 3. Check if email already exists
    existing_user = (
        db.query(User)
        .filter(User.email == email)
        .first()
    )

    if existing_user:
        raise ValueError("User with this email already exists")

    # 4. Check if user_id already exists
    existing_user_id = (
        db.query(User)
        .filter(User.user_id == user_id)
        .first()
    )

    if existing_user_id:
        raise ValueError("User ID already exists")

    # 5. Hash password
    hashed_password = password_hash.hash(password)

    # 6. Create user
    user = User(
        user_id=user_id,
        email=email,
        password_hash=hashed_password,
        role=role.value,
    )

    # 7. Save to database
    db.add(user)
    db.commit()
    db.refresh(user)

    return user


def authenticate_user(
    db: Session,
    email: str,
    password: str,
):
    user = (
        db.query(User)
        .filter(User.email == email)
        .first()
    )

    if not user:
        return None

    if not password_hash.verify(password, user.password_hash):
        return None

    return user


def create_access_token(user: User):
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )

    payload = {
        "sub": str(user.id),
        "role": user.role,
        "exp": expire,
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM,
    )