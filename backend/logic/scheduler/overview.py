"""
The Platform Admin's read-only operational picture of the pool (US-ADM-06; FR-GPU-156, 174).

Node and unit states, queue metadata, utilisation, open alerts, hardware warnings and
the audit log, in one read. Metadata only (A-33): no project reference, snapshot,
client request ID, log or result of any job is read here, so none can leak.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import models as orm
from logic.scheduler import nodes as node_ops
from logic.scheduler.domain import JobState, NodeState

log = logging.getLogger("prayog.scheduler")

ACTIVE_JOB_STATES = (JobState.IN_PROCESS, JobState.QUEUED, JobState.SCHEDULED, JobState.RUNNING)
UTILISATION_WINDOW = timedelta(hours=24)


class UnitState:
    FREE = "FREE"
    ALLOCATED = "ALLOCATED"
    MAINTENANCE = "MAINTENANCE"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class UnitPicture:
    id: int
    index: int
    state: str
    job_id: int | None                       # the job holding it: an ID, never its contents
    maintenance_until: datetime | None


@dataclass(frozen=True)
class NodePicture:
    id: int
    name: str
    owner_lab_id: int
    donation_mode: str
    teaching_pool: bool
    state: str
    state_reason: str | None
    drain_mode: str | None
    last_heartbeat_at: datetime | None
    open_alerts: int
    units: list[UnitPicture]


@dataclass(frozen=True)
class LabQueue:
    lab_id: int
    queued_jobs: int
    queued_gpus: int


@dataclass(frozen=True)
class QueuePicture:
    by_state: dict[str, int]
    by_lab: list[LabQueue]
    oldest_queued_at: datetime | None


@dataclass(frozen=True)
class Utilisation:
    usable_units: int                        # on AVAILABLE nodes, not retired, not in maintenance
    allocated_units: int
    allocated_fraction: float
    gpu_hours_last_24h: float


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    actor: str
    actor_kind: str
    operation: str
    resource_ids: list[str]
    outcome: str
    reason: str | None


@dataclass(frozen=True)
class PoolPicture:
    generated_at: datetime
    nodes: list[NodePicture]
    queue: QueuePicture
    utilisation: Utilisation
    alerts: list[orm.NodeAlert]
    hardware_warnings: dict[str, int] | None      # None when the Steward is not running
    audit: list[AuditEntry] = field(default_factory=list)


def pool_picture(db: Session, now: datetime, audit_limit: int = 50) -> PoolPicture:
    return PoolPicture(
        generated_at=now,
        nodes=_nodes(db, now),
        queue=_queue(db),
        utilisation=_utilisation(db, now),
        alerts=node_ops.open_alerts(db),
        hardware_warnings=hardware_warning_counts(),
        audit=_audit(db, audit_limit),
    )


def _nodes(db: Session, now: datetime) -> list[NodePicture]:
    maintenance = dict(db.execute(
        select(orm.MaintenanceWindow.unit_id, func.max(orm.MaintenanceWindow.ends_at))
        .where(orm.MaintenanceWindow.starts_at <= now, orm.MaintenanceWindow.ends_at > now)
        .group_by(orm.MaintenanceWindow.unit_id)
    ).all())
    holders = dict(db.execute(
        select(orm.Allocation.id, orm.Allocation.job_id)
        .join(orm.GpuUnit, orm.GpuUnit.current_allocation_id == orm.Allocation.id)
    ).all())
    units_by_node: dict[int, list[UnitPicture]] = {}
    for unit in db.scalars(select(orm.GpuUnit).order_by(orm.GpuUnit.node_id, orm.GpuUnit.unit_index)):
        if unit.retired_at is not None:
            state = UnitState.RETIRED
        elif unit.current_allocation_id is not None:
            state = UnitState.ALLOCATED
        elif unit.id in maintenance:
            state = UnitState.MAINTENANCE
        else:
            state = UnitState.FREE
        units_by_node.setdefault(unit.node_id, []).append(UnitPicture(
            id=unit.id, index=unit.unit_index, state=state,
            job_id=holders.get(unit.current_allocation_id), maintenance_until=maintenance.get(unit.id),
        ))
    return [
        NodePicture(
            id=s.node.id, name=s.node.name, owner_lab_id=s.node.owner_lab_id, donation_mode=s.node.donation_mode,
            teaching_pool=s.node.teaching_pool, state=s.node.state, state_reason=s.node.state_reason,
            drain_mode=s.node.drain_mode, last_heartbeat_at=s.node.last_heartbeat_at, open_alerts=s.open_alerts,
            units=units_by_node.get(s.node.id, []),
        )
        for s in node_ops.node_summaries(db)
    ]


def _queue(db: Session) -> QueuePicture:
    by_state = {state: 0 for state in ACTIVE_JOB_STATES}
    by_state.update(db.execute(
        select(orm.Job.state, func.count()).where(orm.Job.state.in_(ACTIVE_JOB_STATES)).group_by(orm.Job.state)
    ).all())
    by_lab = [
        LabQueue(lab_id=lab_id, queued_jobs=jobs, queued_gpus=int(gpus))
        for lab_id, jobs, gpus in db.execute(
            select(orm.Job.lab_id, func.count(), func.coalesce(func.sum(orm.Job.gpus), 0))
            .where(orm.Job.state == JobState.QUEUED).group_by(orm.Job.lab_id).order_by(orm.Job.lab_id)
        ).all()
    ]
    oldest = db.scalar(select(func.min(orm.Job.effective_submit_at)).where(orm.Job.state == JobState.QUEUED))
    return QueuePicture(by_state=by_state, by_lab=by_lab, oldest_queued_at=oldest)


def _utilisation(db: Session, now: datetime) -> Utilisation:
    in_maintenance = (
        select(orm.MaintenanceWindow.id)
        .where(orm.MaintenanceWindow.unit_id == orm.GpuUnit.id,
               orm.MaintenanceWindow.starts_at <= now, orm.MaintenanceWindow.ends_at > now)
        .exists()
    )
    usable, allocated = db.execute(
        select(func.count(), func.count().filter(orm.GpuUnit.current_allocation_id.is_not(None)))
        .select_from(orm.GpuUnit)
        .join(orm.Node, orm.Node.id == orm.GpuUnit.node_id)
        .where(orm.Node.state == NodeState.AVAILABLE, orm.GpuUnit.retired_at.is_(None),
               (orm.GpuUnit.current_allocation_id.is_not(None)) | ~in_maintenance)
    ).one()
    gpu_seconds = db.scalar(
        select(func.coalesce(func.sum(orm.UsageEntry.gpu_seconds), 0.0))
        .where(orm.UsageEntry.ended_at > now - UTILISATION_WINDOW)
    )
    return Utilisation(
        usable_units=usable, allocated_units=allocated,
        allocated_fraction=round(allocated / usable, 4) if usable else 0.0,
        gpu_hours_last_24h=round(float(gpu_seconds) / 3600, 4),
    )


def _audit(db: Session, limit: int) -> list[AuditEntry]:
    rows = db.scalars(select(orm.AuditEvent).order_by(orm.AuditEvent.at.desc(), orm.AuditEvent.id.desc()).limit(limit))
    return [AuditEntry(at=e.at, actor=e.actor, actor_kind=e.actor_kind, operation=e.operation,
                       resource_ids=list(e.resource_ids), outcome=e.outcome, reason=e.reason) for e in rows]


def hardware_warning_counts() -> dict[str, int] | None:
    """Open, resolved and dismissed Steward hardware warnings, or None if the Steward is not running.
    Counts only: the warnings themselves are under /steward/admin/warnings."""
    if os.getenv("STEWARD_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        return None
    try:
        from integration.steward_bridge import get_steward
        from prayog_steward.hardware import list_warnings

        engine = get_steward().engine
        return {state: len(list_warnings(engine, state, limit=1000)) for state in ("OPEN", "RESOLVED", "DISMISSED")}
    except Exception as exc:  # noqa: BLE001 - the picture must render without the Steward
        log.warning("hardware warnings unavailable: %s", exc)
        return None
