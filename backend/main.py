from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session

from database import engine
from schemas.auth import UserRegister
from logic.auth import register_user


app = FastAPI()


def get_db():
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()


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
