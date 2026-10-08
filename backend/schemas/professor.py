from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProfessorCreate(BaseModel):
    """Admin creates a professor account + profile in one step."""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    department: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128)   # temporary password


class ProfessorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    department: str
