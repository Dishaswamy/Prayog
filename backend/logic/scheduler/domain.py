"""
The scheduler's view of jobs and nodes.

These are plain immutable values. The pass loads them from the database once
(the pass snapshot), and the policy functions reason over them without ever
touching the database. The claim transaction re-checks what matters under lock.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum


class AccountType(StrEnum):
    RESEARCH = "research"
    COURSE = "course"


class DonationMode(StrEnum):
    RESERVED = "reserved"
    SHARED = "shared"


class NodeState(StrEnum):
    PENDING_AGENT = "PENDING_AGENT"
    AVAILABLE = "AVAILABLE"
    DRAINING = "DRAINING"
    DOWN = "DOWN"
    DECOMMISSIONING = "DECOMMISSIONING"
    DECOMMISSIONED = "DECOMMISSIONED"


class JobState(StrEnum):
    REJECTED = "REJECTED"
    IN_PROCESS = "IN_PROCESS"
    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WALLTIME_EXCEEDED = "WALLTIME_EXCEEDED"


class ReleaseReason(StrEnum):
    """Why an allocation ended. The first four end the job; the last two put it back in the queue."""
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WALLTIME_EXCEEDED = "WALLTIME_EXCEEDED"
    CANCELLED = "CANCELLED"
    NODE_FAILURE = "NODE_FAILURE"            # node went DOWN: requeued, not charged (FR-GPU-135, 143)
    DISPATCH_TIMEOUT = "DISPATCH_TIMEOUT"    # never acknowledged: requeued, never started (FR-GPU-126)


class PriorityClass(IntEnum):
    """Lower value = higher priority. Classes are settled in this order."""
    P0 = 0  # reserved: the node belongs to the job's lab
    P1 = 1  # burst: another lab's shared node, owner has nothing queued
    P2 = 2  # guest: another lab's shared node, owner has queued work
    P3 = 3  # teaching: course account on a teaching-pool node


class QueueReason(StrEnum):
    """Why a job is still QUEUED after a pass (FR-GPU-144)."""
    NO_ELIGIBLE_NODE = "NO_ELIGIBLE_NODE"
    INSUFFICIENT_FREE_UNITS = "INSUFFICIENT_FREE_UNITS"
    MAINTENANCE_WINDOW = "MAINTENANCE_WINDOW"
    LOST_CLAIM_RACE = "LOST_CLAIM_RACE"
    OUTRANKED = "OUTRANKED"
    OWNER_CLAIM_PENDING = "OWNER_CLAIM_PENDING"


@dataclass(frozen=True)
class MaintenanceWindow:
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class UnitView:
    unit_id: int
    unit_index: int
    free: bool                                   # no live allocation right now
    maintenance: tuple[MaintenanceWindow, ...] = ()


@dataclass(frozen=True)
class NodeView:
    node_id: int
    owner_lab_id: int
    donation_mode: DonationMode
    teaching_pool: bool
    state: NodeState
    units: tuple[UnitView, ...] = field(default_factory=tuple)

    @property
    def unit_count(self) -> int:
        return len(self.units)


@dataclass(frozen=True)
class JobView:
    job_id: int
    lab_id: int                      # acting lab, stamped at submission
    account_type: AccountType
    gpus: int
    walltime_minutes: int
    submit_time: datetime            # the age clock (effective submit time, FR-GPU-071/072)
