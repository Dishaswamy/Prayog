from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProfessorCreate(BaseModel):
    """Admin creates a professor account + profile in one step."""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    department: str = Field(min_length=1, max_length=100)
    # Optional. Omit it and the account gets a random password nobody sees; the
    # professor sets their own through the password reset (forgot-password).
    password: str | None = Field(default=None, min_length=8, max_length=128)
    lab_id: int | None = None                             # the lab this professor runs


class ProfessorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    department: str
    lab_id: int | None


class ProfessorCreatedOut(ProfessorOut):
    """The reply to creating a professor. Shown once: it is never stored in clear."""
    temporary_password: str | None = None      # set only when the server generated one
