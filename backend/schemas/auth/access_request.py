from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


def _to_naive_utc(v: datetime | None) -> datetime | None:
    """
    The DB stores naive datetimes. If the frontend sends "...Z" or "+05:30",
    Pydantic gives a timezone-aware datetime, and comparing aware vs naive
    crashes. Convert everything to naive UTC.
    """
    if v is not None and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class AccessRequestCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    role: Literal["STUDENT", "RESEARCHER"]
    course: str = Field(min_length=1, max_length=100)
    branch: str = Field(min_length=1, max_length=100)
    # Students choose; researchers are always RESEARCH_PROJECT (default if omitted)
    purpose: Literal["COURSE_PROJECT", "RESEARCH_PROJECT"] | None = None
    professor_email: EmailStr
    roll_number: str = Field(min_length=1, max_length=50)   # required for both roles
    start_date: datetime
    end_date: datetime | None = None                        # students only

    @field_validator("role", mode="before")
    @classmethod
    def upper_role(cls, v):
        return v.upper() if isinstance(v, str) else v

    @field_validator("purpose", mode="before")
    @classmethod
    def upper_purpose(cls, v):
        return v.upper() if isinstance(v, str) else v

    @field_validator("start_date", "end_date")
    @classmethod
    def naive_utc(cls, v):
        return _to_naive_utc(v)


class AccessRequestApproval(BaseModel):
    status: Literal["APPROVED", "REJECTED"]

    @field_validator("status", mode="before")
    @classmethod
    def upper_status(cls, v):
        return v.upper() if isinstance(v, str) else v


class AccessRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    role: str
    course: str
    branch: str
    purpose: str
    professor_id: int
    roll_number: str
    start_date: datetime
    end_date: datetime | None
    status_professor: str
    status_admin: str
    final_status: str
    revoke_requested_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    created_at: datetime
    updated_at: datetime
