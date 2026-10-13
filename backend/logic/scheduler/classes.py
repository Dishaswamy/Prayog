"""
Priority classes (GPU requirements section 7.2, FR-GPU-036 to 042).

A class belongs to a (job, node) pair, never to a job alone: the same job can be
P0 on its own lab's node and P2 on another lab's node in the same pass. Users
never choose it. Class dominates score absolutely (I8): scores only order jobs
inside one class.
"""

from collections.abc import Iterable

from logic.scheduler.domain import AccountType, DonationMode, JobView, NodeView, PriorityClass


def owners_with_queued_work(queued_jobs: Iterable[JobView]) -> frozenset[int]:
    """
    Labs with at least one QUEUED job at the start of the pass (FR-GPU-039/040).
    Any queued job counts, whether or not it could run on the node in question
    (accepted as limitation L12).
    """
    return frozenset(j.lab_id for j in queued_jobs if j.account_type == AccountType.RESEARCH)


def resolve_class(job: JobView, node: NodeView, owners_waiting: frozenset[int]) -> PriorityClass | None:
    """
    Class of an eligible pair. Returns None when no class applies, which for an
    eligible pair cannot happen; it is there so a caller that skipped eligibility
    gets no placement rather than a wrong one.
    """
    if job.account_type == AccountType.COURSE:
        return PriorityClass.P3 if node.teaching_pool else None
    if node.teaching_pool:
        return None                                   # research never runs on teaching nodes
    if node.owner_lab_id == job.lab_id:
        return PriorityClass.P0
    if node.donation_mode == DonationMode.SHARED:
        return PriorityClass.P2 if node.owner_lab_id in owners_waiting else PriorityClass.P1
    return None                                       # someone else's reserved node
