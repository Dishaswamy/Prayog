from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from models import User
from logic.dependencies import get_current_user

from database import get_db
from schemas.auth import UserRegister, UserLogin
from logic.auth import (
    register_user,
    authenticate_user,
    create_access_token,
)


app = FastAPI()


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/auth/register")
def register(
    user: UserRegister,
    db: Session = Depends(get_db),
):
    try:
        new_user = register_user(
            db=db,
            email=user.email,
            password=user.password,
            role=user.role,
        )

        return {
            "message": "User registered successfully",
            "user_id": new_user.user_id,
            "email": new_user.email,
            "role": new_user.role,
        }

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )


@app.post("/auth/login")
def login(
    user: UserLogin,
    db: Session = Depends(get_db),
):
    authenticated_user = authenticate_user(
        db=db,
        email=user.email,
        password=user.password,
    )

    if not authenticated_user:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
        )

    access_token = create_access_token(authenticated_user)

    return {
        "access_token": access_token,
        "token_type": "bearer",
    }

@app.get("/auth/me")
def get_me(
    current_user: User = Depends(get_current_user),
):
    return {
        "user_id": current_user.user_id,
        "email": current_user.email,
        "role": current_user.role,
    }