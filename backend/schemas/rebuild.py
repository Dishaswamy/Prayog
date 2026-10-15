from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RebuildStart(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    snapshot_hash: str = Field(min_length=1, max_length=128, description="A snapshot one of your jobs ran")
    gpus: Any = Field(description="Positive whole number of GPUs on one node")
    walltime: Any = Field(description="HH:MM", examples=["01:00"])
    client_request_id: str = Field(min_length=1, max_length=64, description="Same ID on a retry returns the same attempt")


class RebuildOut(BaseModel):
    attempt_id: int
    snapshot_hash: str
    job_id: int
    job_state: str
    state: str                    # REBUILD_INITIATED | REBUILD_SUCCESS | REBUILD_FAILED | REBUILD_CANCELLED
    created_at: datetime
