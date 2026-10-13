"""
Until when a queued job waits for its own lab's GPUs (FR-GPU-108, A-08, A-14).

There is no preemption, so a job blocked on its own lab's node only because the
units are busy waits for those runs to end. Each run is guaranteed to have released
its units by start + walltime + the walltime completion bound, so the k-th earliest
of those times is when k more units are guaranteed free.
"""

from dataclasses import replace
from datetime import datetime

from logic.scheduler.domain import JobView, NodeView
from logic.scheduler.eligibility import is_eligible, usable_units


def p0_guaranteed_free_at(
    job: JobView,
    nodes: list[NodeView],
    busy_until: dict[int, datetime],
    now: datetime,
) -> datetime | None:
    """
    The earliest time one of the job's own-lab nodes is guaranteed to have enough
    usable units, or None: when a node fits it already, when none of its lab's
    nodes would fit even if empty (blocked by something other than occupancy), or
    when the busy units cannot be bounded. busy_until maps an occupied unit to the
    time it is guaranteed free. Units under maintenance are never counted on.
    """
    best: datetime | None = None
    for node in nodes:
        if node.owner_lab_id != job.lab_id:
            continue
        if is_eligible(job, node, now):
            return None                                   # placeable now: nothing to wait for
        empty = replace(node, units=tuple(replace(u, free=True) for u in node.units))
        if not is_eligible(job, empty, now):
            continue                                      # state, pool, size or maintenance: not occupancy
        need = job.gpus - len(usable_units(job, node, now))
        times = sorted(busy_until[u.unit_id] for u in node.units
                       if not u.free and not u.maintenance and u.unit_id in busy_until)
        if len(times) >= need:
            at = times[need - 1]
            best = at if best is None else min(best, at)
    return best
