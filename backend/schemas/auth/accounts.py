from enum import Enum
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# One rule for every password field
Password = Annotated[str, Field(min_length=8, max_length=128)]


class UserRole(str, Enum):
    STUDENT = "STUDENT"
    RESEARCHER = "RESEARCHER"
    PROFESSOR = "PROFESSOR"
    ADMIN = "ADMIN"


class UserRegister(BaseModel):
    email: EmailStr
    password: Password
    # Only students/researchers can self-register.
    # Professors are created by an admin; the first admin by a script.
    role: Literal["STUDENT", "RESEARCHER"]

    @field_validator("role", mode="before")
    @classmethod
    def upper_role(cls, v):
        # Accept "student" / "Student" from the frontend
        return v.upper() if isinstance(v, str) else v


class UserLogin(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Email or user ID (the part of the email before @). Sent as "email" by the
    # login form; "user_id" and "identifier" are accepted too.
    email: str = Field(
        min_length=1, max_length=255,
        validation_alias=AliasChoices("email", "user_id", "identifier"),
    )
    password: str = Field(min_length=1, max_length=128)
    # Role picked on the login screen; must match the account's role
    role: Literal["STUDENT", "RESEARCHER", "PROFESSOR", "ADMIN"]

    @field_validator("role", mode="before")
    @classmethod
    def upper_role(cls, v):
        return v.upper() if isinstance(v, str) else v


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    email: str
    role: str


# ── Forgot / reset password (OTP) ─────────────
class ForgotPasswordIn(BaseModel):
    """Step 1: registered email + the new password -> OTP is emailed."""
    email: EmailStr
    new_password: Password
    confirm_password: str | None = None     # the "type it again" box; checked if sent

    @model_validator(mode="after")
    def passwords_match(self):
        if self.confirm_password is not None and self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class VerifyOtpIn(BaseModel):
    """Step 2: the OTP from the email -> password changes."""
    email: EmailStr
    otp: str = Field(pattern=r"^\d{6}$")    # exactly 6 digits


class MessageOut(BaseModel):
    message: str
