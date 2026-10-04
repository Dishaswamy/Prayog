from datetime import datetime

from pydantic import BaseModel, EmailStr


class AccessRequestCreate(BaseModel):
    name: str
    role: str
    course: str
    branch: str

    professor_name: str
    professor_email: EmailStr

    roll_number: str

    start_date: datetime
    end_date: datetime | None = None