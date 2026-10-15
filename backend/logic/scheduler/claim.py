"""
The claim: turn a ranked (job, node) pair into an allocation, all or nothing
(GPU requirements section 7.4, FR-GPU-051 to FR-GPU-057, FR-GPU-186).

One short transaction:

    1. lock the job row, skipping it if another pass holds it; it must still be QUEUED
    2. share-lock the node row; it must still be AVAILABLE (FR-GPU-118)
    3. lock k free units on the node, lowest index first, FOR UPDATE SKIP LOCKED;
       fewer than k means another pass won them (LOST_CLAIM_RACE, FR-GPU-056)
    4. P1/P2 only: roll back if the owner lab now has a QUEUED job eligible for
       the node (owner-claim guard, FR-GPU-186)
    5. write the allocation and its units, mark the units, set the job SCHEDULED,
       record the decision (FR-GPU-098), commit

Every condition that decides the claim lives on a row this transaction has
locked. Under READ COMMITTED, PostgreSQL re-checks a locked row's WHERE clause
against the row's newest version, but never re-checks conditions on other
tables. So "this unit is free" is the unit's own current_allocation_id, not a
join on allocations. The partial unique indexes are the backstop if a code path
ever skips the locks (FR-GPU-055). The one condition read from another table is
maintenance: a window an admin adds while a claim is in flight is handled like
any window over a unit already in use (FR-GPU-084).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.orm import Session

import models as orm
from logic.scheduler.domain import JobState, NodeState, PriorityClass, QueueReason
from logic.scheduler.eligibility import is_eligible
from logic.scheduler.loading import load_node_view, to_job_view
from logic.scheduler.scoring import ScoreBreakdown

GUARDED_CLASSES = (PriorityClass.P1, PriorityClass.P2)


class ClaimStatus(StrEnum):
    CLAIMED = "CLAIMED"
    JOB_UNAVAILABLE = "JOB_UNAVAILABLE"      # another pass holds the job, or it is no longer QUEUED
    NODE_UNAVAILABLE = "NODE_UNAVAILABLE"    # the node left AVAILABLE after the pass read it
    LOST_CLAIM_RACE = "LOST_CLAIM_RACE"      # fewer than k units could be locked
    OWNER_CLAIM_PENDING = "OWNER_CLAIM_PENDING"


# What a failed claim means for the job's queue reason. JOB_UNAVAILABLE has none:
# the job belongs to whichever transaction holds it.
_QUEUE_REASON = {
    ClaimStatus.NODE_UNAVAILABLE: QueueReason.NO_ELIGIBLE_NODE,
    ClaimStatus.LOST_CLAIM_RACE: QueueReason.LOST_CLAIM_RACE,
    ClaimStatus.OWNER_CLAIM_PENDING: QueueReason.OWNER_CLAIM_PENDING,
}


@dataclass(frozen=True)
class ClaimOutcome:
    status: ClaimStatus
    allocation_id: int | None = None
    unit_ids: tuple[int, ...] = ()

    @property
    def claimed(self) -> bool:
        return self.status == ClaimStatus.CLAIMED

    @property
    def queue_reason(self) -> QueueReason | None:
        return _QUEUE_REASON.get(self.status)


class ClaimInvariantError(RuntimeError):
    """A write that the held locks guarantee did not happen. Never expected."""


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def claim(
    session: Session,
    job_id: int,
    node_id: int,
    cls: PriorityClass,
    breakdown: ScoreBreakdown,
    pass_id: str,
    trigger: str,
    now: datetime | None = None,
) -> ClaimOutcome:
    """
    Claim the job's units on the node. Commits on success and rolls back
    otherwise, so nothing is held after a failed claim (FR-GPU-054). Pass a
    session with no transaction in progress.
    """
    now = now or utc_now()
    try:
        outcome = _claim(session, job_id, node_id, cls, breakdown, pass_id, trigger, now)
    except BaseException:
        session.rollback()
        raise
    if outcome.claimed:
        session.commit()
    else:
        session.rollback()
    return outcome


def _claim(
    session: Session,
    job_id: int,
    node_id: int,
    cls: PriorityClass,
    breakdown: ScoreBreakdown,
    pass_id: str,
    trigger: str,
    now: datetime,
) -> ClaimOutcome:
    # 1. The job. SKIP LOCKED: if another pass is claiming it, leave it to that pass.
    job = session.scalars(
        select(orm.Job)
        .where(orm.Job.id == job_id, orm.Job.state == JobState.QUEUED)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).first()
    if job is None:
        return ClaimOutcome(ClaimStatus.JOB_UNAVAILABLE)

    # 2. The node. A share lock lets claims run side by side but waits for a state change.
    node = session.scalars(
        select(orm.Node)
        .where(orm.Node.id == node_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).first()
    if node is None or node.state != NodeState.AVAILABLE:
        return ClaimOutcome(ClaimStatus.NODE_UNAVAILABLE)

    # 3. The units: free, and no maintenance overlapping [now, now + walltime) (FR-GPU-034).
    job_end = now + timedelta(minutes=job.walltime_minutes)
    in_maintenance = (
        select(orm.MaintenanceWindow.id)
        .where(
            orm.MaintenanceWindow.unit_id == orm.GpuUnit.id,
            orm.MaintenanceWindow.starts_at < job_end,
            orm.MaintenanceWindow.ends_at > now,
        )
        .exists()
    )
    unit_ids = tuple(session.scalars(
        select(orm.GpuUnit.id)
        .where(orm.GpuUnit.node_id == node_id, orm.GpuUnit.current_allocation_id.is_(None),
               orm.GpuUnit.retired_at.is_(None), ~in_maintenance)
        .order_by(orm.GpuUnit.unit_index)
        .limit(job.gpus)
        .with_for_update(skip_locked=True)
    ).all())
    if len(unit_ids) < job.gpus:
        return ClaimOutcome(ClaimStatus.LOST_CLAIM_RACE)

    # 4. Owner-claim guard: a guest may not take a node its owner can use right now.
    if cls in GUARDED_CLASSES and _owner_has_eligible_job(session, node, now):
        return ClaimOutcome(ClaimStatus.OWNER_CLAIM_PENDING)

    # 5. Write everything.
    allocation = orm.Allocation(job_id=job.id, node_id=node_id, claimed_at=now)
    session.add(allocation)
    session.flush()
    session.add_all(orm.AllocationUnit(allocation_id=allocation.id, unit_id=u) for u in unit_ids)

    marked = session.execute(
        update(orm.GpuUnit)
        .where(orm.GpuUnit.id.in_(unit_ids), orm.GpuUnit.current_allocation_id.is_(None))
        .values(current_allocation_id=allocation.id)
        .execution_options(synchronize_session=False)
    ).rowcount
    scheduled = session.execute(
        update(orm.Job)
        .where(orm.Job.id == job.id, orm.Job.state == JobState.QUEUED)
        .values(state=JobState.SCHEDULED, state_reason=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    if marked != len(unit_ids) or scheduled != 1:  # pragma: no cover - the locks above rule it out
        raise ClaimInvariantError(f"job {job.id}: marked {marked}/{len(unit_ids)} units, scheduled {scheduled}")

    session.add(orm.SchedulingDecision(
        pass_id=pass_id,
        trigger=trigger,
        job_id=job.id,
        node_id=node_id,
        allocation_id=allocation.id,
        priority_class=int(cls),
        age=breakdown.age,
        fair_share=breakdown.fair_share,
        contribution=breakdown.contribution,
        size=breakdown.size,
        score=breakdown.score,
        escalated=breakdown.escalated,
    ))
    session.flush()
    return ClaimOutcome(ClaimStatus.CLAIMED, allocation_id=allocation.id, unit_ids=unit_ids)


def _owner_has_eligible_job(session: Session, node: orm.Node, now: datetime) -> bool:
    """
    Does the node's owner lab have a QUEUED job that could run on it now? Tests
    eligibility, not just a non-empty queue, so guests are not blocked by owner
    work that could never use this node (A-59).
    """
    owner_jobs = session.scalars(
        select(orm.Job).where(orm.Job.lab_id == node.owner_lab_id, orm.Job.state == JobState.QUEUED)
    ).all()
    if not owner_jobs:
        return False
    # The units this claim just locked are still free here: the node as it was before the claim.
    view = load_node_view(session, node, now)
    return any(is_eligible(to_job_view(j), view, now) for j in owner_jobs)
