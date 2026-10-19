from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from logic.access_check import check_active_access
from logic.auth import (
    OTP_EXPIRE_MINUTES,
    authenticate_user,
    create_access_token,
    register_user,
    revoke_session,
    request_password_reset_otp,
    verify_password_reset_otp,
)
from logic.dependencies import get_current_user
from logic.email import send_password_reset_otp_email
from models import User
from schemas.auth import (
    ForgotPasswordIn,
    MessageOut,
    TokenOut,
    UserLogin,
    UserOut,
    UserRegister,
    VerifyOtpIn,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(body: UserRegister, db: Session = Depends(get_db)):
    return register_user(db=db, email=body.email, password=body.password, role=body.role)


@router.post("/login", response_model=TokenOut)
def login(body: UserLogin, db: Session = Depends(get_db)):
    # Unknown account, wrong password and wrong role all get the SAME error, so the
    # login form can't be used to discover accounts or which role an email has.
    user = authenticate_user(db=db, identifier=body.email, password=body.password, role=body.role)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email, password or role",
        )
    return TokenOut(access_token=create_access_token(user))


# The terminal (prayog CLI) login. Same credentials and token as /auth/login, but only for users who
# currently have access: approved, not expired, not revoked. The web portal keeps using /auth/login,
# because a student must be able to log in there to REQUEST access in the first place.
# Wrong password / role is still the one generic 401; the access reason is only given to someone who
# proved their password.
@router.post("/cli/login", response_model=TokenOut)
def cli_login(body: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db=db, identifier=body.email, password=body.password, role=body.role)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email, password or role",
        )
    check_active_access(db, user)
    return TokenOut(access_token=create_access_token(user))


# Ends THIS login only (the token in the Authorization header); other sessions keep working.
@router.post("/logout", response_model=MessageOut)
def logout(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    revoke_session(db, current_user)
    return MessageOut(message="Logged out.")


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


# Forgot / reset password, step 1: email + new password -> OTP emailed.
# Same reply whether or not the email exists.
@router.post("/forgot-password", response_model=MessageOut)
def forgot_password(
    body: ForgotPasswordIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    result = request_password_reset_otp(db, body.email, body.new_password)
    if result:
        email, otp = result
        background_tasks.add_task(send_password_reset_otp_email, email, otp, OTP_EXPIRE_MINUTES)
    return MessageOut(message="If an account with that email exists, an OTP has been sent to it.")


# Step 2: email + OTP -> password changed
@router.post("/verify-otp", response_model=MessageOut)
def verify_otp(body: VerifyOtpIn, db: Session = Depends(get_db)):
    verify_password_reset_otp(db, body.email, body.otp)
    return MessageOut(message="Password changed. Please log in with your new password.")
