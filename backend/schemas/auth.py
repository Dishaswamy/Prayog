from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserRole(str, Enum):
    STUDENT = "STUDENT"
    RESEARCHER = "RESEARCHER"
    PROFESSOR = "PROFESSOR"
    ADMIN = "ADMIN"


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    # Only students/researchers can self-register.
    # Professors are created by an admin; the first admin by a script.
    role: Literal["STUDENT", "RESEARCHER"]

    @field_validator("role", mode="before")
    @classmethod
    def upper_role(cls, v):
        # Accept "student" / "Student" from the frontend
        return v.upper() if isinstance(v, str) else v


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    email: str
    role: str
