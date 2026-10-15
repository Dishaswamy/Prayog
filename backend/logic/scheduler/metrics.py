"""
Scheduler metrics (NFR-GPU-051, A-76): queue depth, free units, pass duration,
claim failures and dispatch timeouts, in the Prometheus text format.

Everything is read from the database, so passes run by the tick process and by
API workers are counted alike. Only counts are exposed, never job contents
(NFR-GPU-052).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

import models as orm
from logic.scheduler.domain import JobState, NodeState, ReleaseReason


@dataclass(frozen=True)
class Metrics:
    queue_depth: int
    free_units: int
    passes: int
    pass_seconds_total: float
    last_pass_seconds: float
    claim_failures: int
    dispatch_timeouts: int


def collect(db: Session, now: datetime) -> Metrics:
    queue_depth = db.scalar(select(func.count()).select_from(orm.Job).where(orm.Job.state == JobState.QUEUED))

    in_maintenance = exists().where(
        orm.MaintenanceWindow.unit_id == orm.GpuUnit.id,
        orm.MaintenanceWindow.starts_at <= now,
        orm.MaintenanceWindow.ends_at > now,
    )
    free_units = db.scalar(
        select(func.count())
        .select_from(orm.GpuUnit)
        .join(orm.Node, orm.Node.id == orm.GpuUnit.node_id)
        .where(orm.Node.state == NodeState.AVAILABLE, orm.GpuUnit.current_allocation_id.is_(None),
               orm.GpuUnit.retired_at.is_(None), ~in_maintenance)
    )

    passes, duration_ms, failures = db.execute(select(
        func.count(), func.coalesce(func.sum(orm.SchedulingPass.duration_ms), 0.0),
        func.coalesce(func.sum(orm.SchedulingPass.claim_failures), 0),
    )).one()
    last_ms = db.scalar(
        select(orm.SchedulingPass.duration_ms).order_by(orm.SchedulingPass.started_at.desc()).limit(1)
    ) or 0.0

    dispatch_timeouts = db.scalar(
        select(func.count()).select_from(orm.Allocation)
        .where(orm.Allocation.release_reason == ReleaseReason.DISPATCH_TIMEOUT)
    )
    return Metrics(
        queue_depth=queue_depth,
        free_units=free_units,
        passes=passes,
        pass_seconds_total=duration_ms / 1000,
        last_pass_seconds=last_ms / 1000,
        claim_failures=int(failures),
        dispatch_timeouts=dispatch_timeouts,
    )


def render(m: Metrics) -> str:
    """Prometheus text exposition format, version 0.0.4."""
    lines = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[str, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{sample} {value:g}" for sample, value in samples)

    metric("prayog_queue_depth", "gauge", "Jobs waiting in QUEUED.", [("prayog_queue_depth", m.queue_depth)])
    metric("prayog_free_units", "gauge", "Unallocated GPU units on AVAILABLE nodes, outside maintenance.",
           [("prayog_free_units", m.free_units)])
    metric("prayog_pass_duration_seconds", "summary", "Scheduling pass duration.",
           [("prayog_pass_duration_seconds_sum", m.pass_seconds_total),
            ("prayog_pass_duration_seconds_count", m.passes)])
    metric("prayog_last_pass_duration_seconds", "gauge", "Duration of the most recent scheduling pass.",
           [("prayog_last_pass_duration_seconds", m.last_pass_seconds)])
    metric("prayog_claim_failures_total", "counter", "Claim attempts that did not place the job.",
           [("prayog_claim_failures_total", m.claim_failures)])
    metric("prayog_dispatch_timeouts_total", "counter", "Allocations released because the agent never acknowledged.",
           [("prayog_dispatch_timeouts_total", m.dispatch_timeouts)])
    return "\n".join(lines) + "\n"
