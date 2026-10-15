"""
Rows to views: builds the immutable values the policy functions reason over
(logic.scheduler.domain, logic.scheduler.scoring) from the scheduling tables.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

import models as orm
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import (
    AccountType,
    DonationMode,
    JobState,
    JobView,
    MaintenanceWindow,
    NodeState,
    NodeView,
    UnitView,
)
from logic.scheduler.scoring import LabUsage, decayed


def to_job_view(job: orm.Job) -> JobView:
    return JobView(
        job_id=job.id,
        lab_id=job.lab_id,
        account_type=AccountType(job.account_type),
        gpus=job.gpus,
        walltime_minutes=job.walltime_minutes,
        submit_time=job.effective_submit_at,
    )


def load_queued_jobs(session: Session) -> list[JobView]:
    jobs = session.scalars(select(orm.Job).where(orm.Job.state == JobState.QUEUED).order_by(orm.Job.id))
    return [to_job_view(j) for j in jobs]


def load_nodes(session: Session, now: datetime, node_ids: Iterable[int] | None = None) -> list[NodeView]:
    """
    Nodes with their units, each free or not, and the maintenance windows not yet
    over. Without node_ids: every node that is not DECOMMISSIONED. Three queries
    whatever the pool size.
    """
    query = select(orm.Node).order_by(orm.Node.id)
    if node_ids is None:
        query = query.where(orm.Node.state != NodeState.DECOMMISSIONED)
    else:
        query = query.where(orm.Node.id.in_(list(node_ids)))
    nodes = session.scalars(query).all()

    units_by_node: dict[int, list[orm.GpuUnit]] = defaultdict(list)
    for unit in session.scalars(
        select(orm.GpuUnit)
        .where(orm.GpuUnit.node_id.in_([n.id for n in nodes]), orm.GpuUnit.retired_at.is_(None))
        .order_by(orm.GpuUnit.unit_index)
    ):
        units_by_node[unit.node_id].append(unit)

    windows: dict[int, list[MaintenanceWindow]] = defaultdict(list)
    unit_ids = [u.id for units in units_by_node.values() for u in units]
    rows = session.execute(
        select(orm.MaintenanceWindow.unit_id, orm.MaintenanceWindow.starts_at, orm.MaintenanceWindow.ends_at)
        .where(orm.MaintenanceWindow.unit_id.in_(unit_ids), orm.MaintenanceWindow.ends_at > now)
    )
    for unit_id, starts_at, ends_at in rows:
        windows[unit_id].append(MaintenanceWindow(starts_at=starts_at, ends_at=ends_at))

    return [
        NodeView(
            node_id=node.id,
            owner_lab_id=node.owner_lab_id,
            donation_mode=DonationMode(node.donation_mode),
            teaching_pool=node.teaching_pool,
            state=NodeState(node.state),
            units=tuple(
                UnitView(
                    unit_id=u.id,
                    unit_index=u.unit_index,
                    free=u.current_allocation_id is None,
                    maintenance=tuple(windows[u.id]),
                )
                for u in units_by_node[node.id]
            ),
        )
        for node in nodes
    ]


def load_node_view(session: Session, node: orm.Node, now: datetime) -> NodeView:
    [view] = load_nodes(session, now, [node.id])
    return view


def load_usage(
    session: Session, now: datetime, cfg: SchedulerConfig = DEFAULT_CONFIG
) -> tuple[dict[int, LabUsage], float]:
    """
    Each lab's usage when the pass starts, and the pool's total decayed usage.
    Decayed usage counts everything a lab was charged for (FR-GPU-047); served and
    consumed count only runs across labs inside the contribution window
    (FR-GPU-048). An uncharged (interrupted) run still counts as served for the
    host lab (FR-GPU-143). A course job's lab is its sponsorship group (FR-GPU-120).
    """
    window_start = now - timedelta(days=cfg.contribution_window_days)
    decayed_hours: dict[int, float] = defaultdict(float)
    served: dict[int, float] = defaultdict(float)
    consumed: dict[int, float] = defaultdict(float)

    rows = session.execute(select(
        orm.UsageEntry.consumer_lab_id, orm.UsageEntry.provider_lab_id,
        orm.UsageEntry.gpu_seconds, orm.UsageEntry.ended_at, orm.UsageEntry.charged,
    ))
    for consumer, provider, gpu_seconds, ended_at, charged in rows:
        hours = gpu_seconds / 3600
        if charged:
            decayed_hours[consumer] += decayed(hours, (now - ended_at).total_seconds(), cfg)
        if ended_at > window_start and consumer != provider:
            served[provider] += hours
            if charged:
                consumed[consumer] += hours

    labs = decayed_hours.keys() | served.keys()
    usage = {lab: LabUsage(decayed_hours[lab], served[lab], consumed[lab]) for lab in labs}
    return usage, sum(decayed_hours.values())
