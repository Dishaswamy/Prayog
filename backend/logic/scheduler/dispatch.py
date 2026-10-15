"""
Dispatch to node agents (GPU requirements section 7.5; architecture section 7.5,
ADR-06; FR-GPU-059 to 064, 069, 126, 128, 129; NFR-GPU-019).

Pull model: nothing is pushed to an agent. An agent asks for the live allocations on
its own node; the committed allocation row is the dispatch, so a crash between claim
and dispatch loses nothing. The allocation ID is the idempotency key (FR-GPU-060).

    allocations_for(node)            what the agent should be running, or stopping
    acknowledge(node, allocation)    the agent confirms the start; refused (409) if the
                                     job was cancelled or requeued meanwhile, so the
                                     agent must not start it
    report(node, allocation, ...)    the process ended and the workspace is unmounted:
                                     release the units (FR-GPU-069)

Every call is scoped to the calling node (FR-GPU-129): another node's allocation is
reported as not found.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import BadRequestError, ConflictError, NotFoundError
from logic.scheduler.claim import utc_now
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import ReleaseReason
from logic.scheduler.lifecycle import ReleaseResult, mark_running, release


class Outcome(StrEnum):
    """How the agent says a run ended."""
    EXITED = "EXITED"            # the process exited on its own, with an exit code
    WALLTIME = "WALLTIME"        # the agent's own walltime timer stopped it (FR-GPU-134)
    STOPPED = "STOPPED"          # stopped because the server asked (cancel, maintenance, drain)


@dataclass(frozen=True)
class AllocationOrder:
    allocation_id: int
    job_id: int
    unit_indexes: tuple[int, ...]
    walltime_minutes: int
    snapshot_hash: str | None
    started_at: datetime | None          # None until acknowledged
    stop_reason: str | None              # set when the server wants the run stopped
    walltime_ends_at: datetime | None


def allocations_for(db: Session, node_id: int) -> list[AllocationOrder]:
    rows = db.execute(
        select(orm.Allocation, orm.Job.walltime_minutes, orm.Job.snapshot_hash)
        .join(orm.Job, orm.Job.id == orm.Allocation.job_id)
        .where(orm.Allocation.node_id == node_id, orm.Allocation.released_at.is_(None))
        .order_by(orm.Allocation.id)
    ).all()
    units: dict[int, list[int]] = {}
    for allocation_id, index in db.execute(
        select(orm.AllocationUnit.allocation_id, orm.GpuUnit.unit_index)
        .join(orm.GpuUnit, orm.GpuUnit.id == orm.AllocationUnit.unit_id)
        .where(orm.AllocationUnit.allocation_id.in_([a.id for a, _, _ in rows]))
        .order_by(orm.GpuUnit.unit_index)
    ):
        units.setdefault(allocation_id, []).append(index)
    return [
        AllocationOrder(
            allocation_id=a.id,
            job_id=a.job_id,
            unit_indexes=tuple(units.get(a.id, ())),
            walltime_minutes=walltime,
            snapshot_hash=snapshot,
            started_at=a.acknowledged_at,
            stop_reason=a.stop_reason,
            walltime_ends_at=a.acknowledged_at + timedelta(minutes=walltime) if a.acknowledged_at else None,
        )
        for a, walltime, snapshot in rows
    ]


def _own_allocation(db: Session, node_id: int, allocation_id: int) -> orm.Allocation:
    allocation = db.get(orm.Allocation, allocation_id)
    if allocation is None or allocation.node_id != node_id:
        raise NotFoundError(f"Allocation {allocation_id} not found.")      # never confirm another node's IDs
    return allocation


def acknowledge(db: Session, node_id: int, allocation_id: int, now: datetime | None = None) -> bool:
    """
    The agent is starting this allocation. Returns False if it was already acknowledged
    (a repeated delivery after an agent restart: nothing changes, NFR-GPU-019). Refused
    if the allocation was released (the job was cancelled or requeued meanwhile).
    """
    now = now or utc_now()
    allocation = _own_allocation(db, node_id, allocation_id)
    if allocation.released_at is not None:
        raise ConflictError(f"Allocation {allocation_id} is no longer live; do not start it.")
    if allocation.acknowledged_at is not None:
        return False
    mark_running(db, allocation.job_id, now)
    return True


def report(
    db: Session,
    node_id: int,
    allocation_id: int,
    seq: int,
    outcome: Outcome,
    exit_code: int | None = None,
    now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> ReleaseResult | None:
    """
    The run on this allocation ended and its workspace is unmounted: release it. Each
    report carries the agent's sequence number and is applied at most once (FR-GPU-128);
    a repeat returns None. A stop the server asked for decides the reason; otherwise
    the process's own exit does.
    """
    now = now or utc_now()
    outcome = Outcome(outcome)
    if outcome == Outcome.EXITED and exit_code is None:
        raise BadRequestError("An EXITED report needs the exit code.")

    node = db.scalars(
        select(orm.Node).where(orm.Node.id == node_id).with_for_update().execution_options(populate_existing=True)
    ).one()
    if seq <= node.last_report_seq:
        db.rollback()
        return None                                         # already applied
    allocation = _own_allocation(db, node_id, allocation_id)
    if allocation.acknowledged_at is None:
        db.rollback()
        raise ConflictError(f"Allocation {allocation_id} was never started.")

    if allocation.stop_reason is not None:
        reason = ReleaseReason(allocation.stop_reason)
    elif outcome == Outcome.WALLTIME:
        reason = ReleaseReason.WALLTIME_EXCEEDED
    elif outcome == Outcome.EXITED:
        reason = ReleaseReason.COMPLETED if exit_code == 0 else ReleaseReason.FAILED
    else:
        reason = ReleaseReason.FAILED                       # stopped without being asked to
    node.last_report_seq = seq                              # committed together with the release
    return release(db, allocation.job_id, reason, now, exit_code=exit_code, cfg=cfg)
