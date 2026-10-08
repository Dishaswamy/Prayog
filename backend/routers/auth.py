from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from logic.auth import authenticate_user, create_access_token, register_user
from logic.dependencies import get_current_user
from models import User
from schemas.auth import TokenOut, UserLogin, UserOut, UserRegister

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(body: UserRegister, db: Session = Depends(get_db)):
    return register_user(db=db, email=body.email, password=body.password, role=body.role)


@router.post("/login", response_model=TokenOut)
def login(body: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db=db, email=body.email, password=body.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    return TokenOut(access_token=create_access_token(user))


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user
