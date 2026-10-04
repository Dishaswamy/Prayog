from enum import Enum
from pydantic import BaseModel, EmailStr


class UserRole(str, Enum):
    student = "student"
    researcher = "researcher"
    professor = "professor"
    admin = "admin"


class UserRegister(BaseModel):
    email: EmailStr
    password: str
    role: UserRole

class UserLogin(BaseModel):
    email: EmailStr
    password: str