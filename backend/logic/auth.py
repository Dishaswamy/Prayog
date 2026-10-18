import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from logic.errors import BadRequestError, ConflictError, ForbiddenError
from models import PasswordResetOTP, User

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY is not configured")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
OTP_EXPIRE_MINUTES = 10
OTP_MAX_ATTEMPTS = 5          # wrong guesses before the OTP is cancelled
OTP_RESEND_COOLDOWN_SECONDS = 60

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


# Checked against when the account doesn't exist, so a missing account takes as long
# to reject as a wrong password and login can't be used to discover accounts.
_DUMMY_HASH = password_hash.hash(secrets.token_urlsafe(16))


def generate_random_password() -> str:
    """Unguessable password nobody is told. New professors set their own via password reset."""
    return secrets.token_urlsafe(32)


def authenticate_user(
    db: Session, identifier: str, password: str, role: str | None = None
) -> User | None:
    """
    Log in with email or user ID, the password, and the role picked on the login
    screen. Returns None for any failure (unknown account, wrong password, wrong
    role) so the caller can give one identical error for all of them.
    """
    identifier = normalize_email(identifier)        # strip + lowercase works for both forms
    column = User.email if "@" in identifier else User.user_id
    user = db.query(User).filter(column == identifier).first()

    # Always verify a hash, found or not
    password_ok = password_hash.verify(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not password_ok:
        return None
    if role is not None and user.role != role.upper():
        return None
    return user


def create_access_token(user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),     # must be a string for PyJWT >= 2.10
        "jti": secrets.token_urlsafe(24),
        "ver": user.token_version,   # old tokens die when the password changes
        "role": user.role,       # informational only; the DB role is what we trust
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ─────────────────────────────────────────────
# Forgot / reset password with an emailed OTP
#   Step 1: email + new password  -> OTP emailed
#   Step 2: email + OTP           -> password changed
# ─────────────────────────────────────────────
INVALID_OTP = "Invalid or expired OTP"


def _utcnow() -> datetime:
    # naive UTC, same as the DB columns
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_otp(user_id: int, otp: str) -> str:
    # Keyed with the server secret + user id, so the DB alone can't be used to
    # brute-force the 6 digits, and the same OTP for two users hashes differently.
    return hmac.new(SECRET_KEY.encode(), f"{user_id}:{otp}".encode(), hashlib.sha256).hexdigest()


def request_password_reset_otp(db: Session, email: str, new_password: str) -> tuple[str, str] | None:
    """
    Step 1. Returns (email, otp) to send, or None if nothing should be sent.
    The route always gives the same reply, so this can't be used to find out
    which emails have accounts.
    """
    # Hash first, for every email, so an unknown email takes as long as a known one
    new_password_hash = password_hash.hash(new_password)

    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if not user:
        return None

    now = _utcnow()

    latest = (
        db.query(PasswordResetOTP)
        .filter(PasswordResetOTP.user_id == user.id)
        .order_by(PasswordResetOTP.id.desc())
        .first()
    )
    # Stop email spam: at most one OTP per minute per user. Silently skip
    # (no error), otherwise the error would reveal that the account exists.
    if latest and (now - latest.created_at).total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
        return None

    # Only the newest OTP should work: cancel older unused ones
    db.query(PasswordResetOTP).filter(
        PasswordResetOTP.user_id == user.id,
        PasswordResetOTP.used_at.is_(None),
    ).update({PasswordResetOTP.used_at: now})

    otp = f"{secrets.randbelow(1_000_000):06d}"     # 6 digits, cryptographically random
    db.add(PasswordResetOTP(
        user_id=user.id,
        otp_hash=_hash_otp(user.id, otp),
        new_password_hash=new_password_hash,
        attempts=0,
        expires_at=now + timedelta(minutes=OTP_EXPIRE_MINUTES),
        created_at=now,
    ))
    db.commit()

    return user.email, otp


def verify_password_reset_otp(db: Session, email: str, otp: str) -> None:
    """Step 2. Correct OTP -> the new password from step 1 becomes the password."""
    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if not user:
        raise BadRequestError(INVALID_OTP)

    record = (
        db.query(PasswordResetOTP)
        .filter(
            PasswordResetOTP.user_id == user.id,
            PasswordResetOTP.used_at.is_(None),
        )
        .order_by(PasswordResetOTP.id.desc())
        .with_for_update()          # two submits at once can't both use it
        .first()
    )
    now = _utcnow()
    if not record or record.expires_at < now:
        raise BadRequestError(INVALID_OTP)

    if not hmac.compare_digest(record.otp_hash, _hash_otp(user.id, otp)):
        record.attempts += 1
        if record.attempts >= OTP_MAX_ATTEMPTS:
            record.used_at = now     # too many wrong guesses: OTP cancelled
            db.commit()
            raise BadRequestError("Too many wrong attempts. Please request a new OTP.")
        db.commit()
        raise BadRequestError(INVALID_OTP)

    user.password_hash = record.new_password_hash
    user.token_version += 1          # logs out every existing session of this user
    record.used_at = now             # OTP can't be used again
    db.commit()
