"""
Rows to views: builds the immutable values the policy functions reason over
(scheduler.domain) from the scheduling tables.
"""

from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from scheduler import models as orm
from scheduler.domain import (
    AccountType,
    DonationMode,
    JobView,
    MaintenanceWindow,
    NodeState,
    NodeView,
    UnitView,
)


def to_job_view(job: orm.Job) -> JobView:
    return JobView(
        job_id=job.id,
        lab_id=job.lab_id,
        account_type=AccountType(job.account_type),
        gpus=job.gpus,
        walltime_minutes=job.walltime_minutes,
        submit_time=job.effective_submit_at,
    )


def load_node_view(session: Session, node: orm.Node, now: datetime) -> NodeView:
    """The node with its units, each free or not, and the maintenance windows not yet over."""
    units = session.scalars(
        select(orm.GpuUnit).where(orm.GpuUnit.node_id == node.id).order_by(orm.GpuUnit.unit_index)
    ).all()

    windows: dict[int, list[MaintenanceWindow]] = defaultdict(list)
    rows = session.execute(
        select(orm.MaintenanceWindow.unit_id, orm.MaintenanceWindow.starts_at, orm.MaintenanceWindow.ends_at)
        .where(orm.MaintenanceWindow.unit_id.in_([u.id for u in units]), orm.MaintenanceWindow.ends_at > now)
    )
    for unit_id, starts_at, ends_at in rows:
        windows[unit_id].append(MaintenanceWindow(starts_at=starts_at, ends_at=ends_at))

    return NodeView(
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
            for u in units
        ),
    )
