from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HandoverPush(BaseModel):
    professor_id: int = Field(gt=0)


class HandoverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    handover_id: str
    project_id: str
    source_owner_id: int
    target_professor_id: int
    status: str
    artifact_inventory: list[dict[str, Any]]
    failed_artifacts: list[str]
    created_at: datetime
    decided_at: datetime | None
