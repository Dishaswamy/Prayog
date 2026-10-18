"""Frozen copy of the original scheduling pass, used only by test_pass_equivalence.py as the reference."""

from dataclasses import replace
from datetime import datetime

from sqlalchemy.orm import Session

from logic.scheduler.claim import ClaimOutcome, ClaimStatus, claim
from logic.scheduler.classes import owners_with_queued_work, resolve_class
from logic.scheduler.domain import JobView, NodeView, PriorityClass, QueueReason
from logic.scheduler.eligibility import ineligibility_reason, is_eligible, usable_units
from logic.scheduler.passes import PassResult
from logic.scheduler.scoring import ScoreBreakdown, rank_key


class ReferencePass:
    """The scheduling pass exactly as it was before pairs were bucketed once (every round re-scanned all jobs x nodes).
    Kept as the oracle that the optimised pass must match, claim for claim."""

    def __init__(
        self,
        session: Session,
        pass_id: str,
        trigger: str,
        now: datetime,
        jobs: list[JobView],
        nodes: list[NodeView],
        breakdowns: dict[int, ScoreBreakdown],
    ):
        self.session = session
        self.pass_id = pass_id
        self.trigger = trigger
        self.now = now
        self.jobs = jobs
        self.snapshot = nodes                                   # as read, for queue reasons
        self.nodes = {n.node_id: n for n in nodes}              # kept current with this pass's claims
        self.breakdowns = breakdowns
        # Owners waiting is fixed at the start of the pass (FR-GPU-039/040); the claim's
        # owner guard covers owners who submit after this point (FR-GPU-186).
        self.owners_waiting = owners_with_queued_work(jobs)

        self.placed: dict[int, ClaimOutcome] = {}
        self.taken_elsewhere: set[int] = set()                  # another pass holds the job
        self.claim_failures: dict[int, QueueReason] = {}
        self.failed_claims = 0
        self.pairs_per_class: dict[PriorityClass, int] = {}

    def run(self) -> PassResult:
        for cls in PriorityClass:                               # P0 settled pool-wide before P1, ... (FR-GPU-043)
            self._round(cls)
        return PassResult(
            pass_id=self.pass_id,
            trigger=self.trigger,
            queued=len(self.jobs),
            pairs_per_class=self.pairs_per_class,
            placed=self.placed,
            reasons=self._queue_reasons(),
            failed_claims=self.failed_claims,
        )

    def _round(self, cls: PriorityClass) -> None:
        pairs = {
            job.job_id: [n.node_id for n in self.nodes.values() if self._pair_is(job, n, cls)]
            for job in self.jobs
            if job.job_id not in self.placed and job.job_id not in self.taken_elsewhere
        }
        self.pairs_per_class[cls] = sum(len(nodes) for nodes in pairs.values())
        candidates = [j for j in self.jobs if pairs.get(j.job_id)]

        for job in sorted(candidates, key=lambda j: rank_key(j, self.breakdowns[j.job_id])):   # FR-GPU-044
            for node_id in self._best_fit(job, pairs[job.job_id]):
                outcome = claim(
                    self.session, job.job_id, node_id, cls, self.breakdowns[job.job_id],
                    pass_id=self.pass_id, trigger=self.trigger, now=self.now,
                )
                if outcome.claimed:
                    self.placed[job.job_id] = outcome
                    self._take(node_id, outcome.unit_ids)
                    break
                self.failed_claims += 1
                if outcome.status == ClaimStatus.JOB_UNAVAILABLE:
                    self.taken_elsewhere.add(job.job_id)
                    break
                self.claim_failures[job.job_id] = outcome.queue_reason

    def _pair_is(self, job: JobView, node: NodeView, cls: PriorityClass) -> bool:
        return is_eligible(job, node, self.now) and resolve_class(job, node, self.owners_waiting) == cls

    def _best_fit(self, job: JobView, node_ids: list[int]) -> list[int]:
        """Nodes still eligible after earlier claims, fewest usable units first, then node id (FR-GPU-122)."""
        still = [self.nodes[n] for n in node_ids if is_eligible(job, self.nodes[n], self.now)]
        return [n.node_id for n in sorted(still, key=lambda n: (len(usable_units(job, n, self.now)), n.node_id))]

    def _take(self, node_id: int, unit_ids: tuple[int, ...]) -> None:
        node = self.nodes[node_id]
        units = tuple(replace(u, free=False) if u.unit_id in unit_ids else u for u in node.units)
        self.nodes[node_id] = replace(node, units=units)

    def _queue_reasons(self) -> dict[int, QueueReason]:
        reasons = {}
        for job in self.jobs:
            if job.job_id in self.placed or job.job_id in self.taken_elsewhere:
                continue
            if job.job_id in self.claim_failures:
                reasons[job.job_id] = self.claim_failures[job.job_id]
            elif any(is_eligible(job, n, self.now) for n in self.snapshot):
                reasons[job.job_id] = QueueReason.OUTRANKED       # it fitted until higher-ranked jobs took the units
            else:
                reasons[job.job_id] = ineligibility_reason(job, self.snapshot, self.now)
        return reasons
