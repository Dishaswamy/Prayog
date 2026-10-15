from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _naive_utc(v: datetime | None) -> datetime | None:
    """The database stores naive UTC; convert any timezone-aware input (as schemas/access_request.py does)."""
    if v is not None and v.tzinfo is not None:
        return v.astimezone(UTC).replace(tzinfo=None)
    return v


class NodeRegister(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    owner_lab_id: int
    donation_mode: Literal["reserved", "shared"]
    teaching_pool: bool = False
    unit_count: int = Field(ge=1, le=64)


class NodeOut(BaseModel):
    id: int
    name: str
    owner_lab_id: int
    donation_mode: str
    teaching_pool: bool
    state: str
    state_reason: str | None
    drain_mode: str | None
    return_requested: bool
    unit_count: int
    free_units: int
    reported_unit_count: int | None
    last_heartbeat_at: datetime | None
    open_alerts: int


class NodeRegistered(BaseModel):
    node: NodeOut
    enrolment_token: str                 # give it to the node's agent; valid once, for 24 hours


class Reason(BaseModel):
    reason: str = Field(min_length=1, max_length=255)


class DrainIn(Reason):
    mode: Literal["soft", "hard"] = "soft"


class SharingIn(Reason):
    donation_mode: Literal["reserved", "shared"] | None = None
    teaching_pool: bool | None = None              # only a shared node may be in the teaching pool


class UnitCountIn(BaseModel):
    unit_count: int = Field(ge=1, le=64)


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: int
    kind: str
    detail: str
    raised_at: datetime


class MaintenancePreviewIn(BaseModel):
    unit_ids: list[int] = Field(min_length=1)
    starts_at: datetime

    @field_validator("starts_at")
    @classmethod
    def utc_start(cls, v):
        return _naive_utc(v)


class MaintenanceBookIn(MaintenancePreviewIn):
    ends_at: datetime
    reason: str = Field(min_length=1, max_length=255)
    confirm: bool = False                # required when running jobs would be stopped (FR-GPU-084)

    @field_validator("ends_at")
    @classmethod
    def utc_end(cls, v):
        return _naive_utc(v)


class MaintenanceMoveIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    confirm: bool = False

    @field_validator("starts_at", "ends_at")
    @classmethod
    def utc_times(cls, v):
        return _naive_utc(v)


class CollisionOut(BaseModel):
    job_id: int
    owner: str
    lab_id: int
    gpu_hours_at_risk: float
    guaranteed_end: datetime


class PreviewOut(BaseModel):
    clear_by: datetime                   # every run on these units is guaranteed to have ended (FR-GPU-083)
    collisions: list[CollisionOut]       # runs the window would stop (FR-GPU-084)


class BookingOut(BaseModel):
    booking_id: str
    unit_ids: list[int]
    starts_at: datetime
    ends_at: datetime
    reason: str
    collisions: list[CollisionOut] = []
