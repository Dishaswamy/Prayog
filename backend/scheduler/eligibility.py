"""
Eligibility: the hard filter on a (job, node) pair (GPU requirements section 7.1).

    eligible(j, n) = n.state == AVAILABLE
                     and usable_units(j, n) >= j.gpus
                     and n.teaching_pool == (j.account == course)
                     and (n.donation_mode == shared or n.owner_lab == j.lab)

Each rule is evaluated independently and reports its own reason (Specification
pattern), so a queued job can be told every reason it is blocked rather than
only the first one (FR-GPU-021, NFR-GPU-027).
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from scheduler.domain import (
    AccountType,
    DonationMode,
    JobView,
    NodeState,
    NodeView,
    QueueReason,
    UnitView,
)


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[QueueReason, ...]   # empty when eligible
    usable_units: int


def _blocked_by_maintenance(unit: UnitView, now: datetime, job_end: datetime) -> bool:
    # A window blocks the unit if it overlaps [now, now + walltime):
    # it starts before the job would end and has not already finished.
    return any(w.starts_at < job_end and w.ends_at > now for w in unit.maintenance)


def usable_units(job: JobView, node: NodeView, now: datetime) -> list[UnitView]:
    """Units free now with no maintenance starting before now + walltime (FR-GPU-034)."""
    job_end = now + timedelta(minutes=job.walltime_minutes)
    return [u for u in node.units if u.free and not _blocked_by_maintenance(u, now, job_end)]


def right_pool(job: JobView, node: NodeView) -> bool:
    """Teaching-pool exact match, both directions (FR-GPU-032)."""
    return node.teaching_pool == (job.account_type == AccountType.COURSE)


def may_use(job: JobView, node: NodeView) -> bool:
    """Reserved nodes are for the owner lab only (FR-GPU-033)."""
    return node.donation_mode == DonationMode.SHARED or node.owner_lab_id == job.lab_id


def reachable(job: JobView, node: NodeView) -> bool:
    """Could this job EVER run here, ignoring current load and node state?"""
    return right_pool(job, node) and may_use(job, node) and node.unit_count >= job.gpus


def evaluate(job: JobView, node: NodeView, now: datetime) -> EligibilityResult:
    reasons: list[QueueReason] = []

    if node.state != NodeState.AVAILABLE:                        # FR-GPU-118
        reasons.append(QueueReason.NO_ELIGIBLE_NODE)
    if not right_pool(job, node) or not may_use(job, node):
        reasons.append(QueueReason.NO_ELIGIBLE_NODE)
    if node.unit_count < job.gpus:                               # single node only (FR-GPU-183)
        reasons.append(QueueReason.NO_ELIGIBLE_NODE)

    usable = usable_units(job, node, now)
    if len(usable) < job.gpus and node.unit_count >= job.gpus:
        free_now = sum(1 for u in node.units if u.free)
        # Enough units are free but maintenance blocks some of them, or not enough are free at all.
        reasons.append(
            QueueReason.MAINTENANCE_WINDOW if free_now >= job.gpus else QueueReason.INSUFFICIENT_FREE_UNITS
        )

    unique = tuple(dict.fromkeys(reasons))  # keep order, drop duplicates
    return EligibilityResult(eligible=not unique, reasons=unique, usable_units=len(usable))


def is_eligible(job: JobView, node: NodeView, now: datetime) -> bool:
    return evaluate(job, node, now).eligible


# When several reasons tie, prefer the one the user can act on or wait out.
_REASON_PRIORITY = [
    QueueReason.INSUFFICIENT_FREE_UNITS,
    QueueReason.MAINTENANCE_WINDOW,
    QueueReason.NO_ELIGIBLE_NODE,
]


def ineligibility_reason(job: JobView, nodes: list[NodeView], now: datetime) -> QueueReason:
    """
    Reason for a job that is eligible on no node: the reason shared by the most
    nodes it could ever reach. A job that can reach no node at all is
    NO_ELIGIBLE_NODE.
    """
    counts: Counter[QueueReason] = Counter()
    for node in nodes:
        if not reachable(job, node):
            continue
        result = evaluate(job, node, now)
        if result.reasons:
            counts[min(result.reasons, key=_REASON_PRIORITY.index)] += 1
    if not counts:
        return QueueReason.NO_ELIGIBLE_NODE
    return min(counts, key=lambda r: (-counts[r], _REASON_PRIORITY.index(r)))
