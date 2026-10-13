"""
The scheduling pass (GPU requirements section 7.4, FR-GPU-043, FR-GPU-121 to 124, FR-GPU-144).

    for cls in P0, P1, P2, P3:
        pairs = every (job, node) with the job QUEUED, eligible, and class(job, node) == cls
        for job in pairs, ordered by rank_key (escalated, score, submit time, job id):
            for node the job can use at cls, ordered by (fewest usable units, node id):
                if claim(job, node) succeeds: stop considering the job

The pass reads one snapshot and keeps it current with its own claims, so a
lower-ranked job sees the units a higher-ranked one just took. Claims by other
passes running at the same time it learns about only from the claim itself,
which is what keeps concurrent passes safe without a global lock. Every job
still QUEUED afterwards gets a reason (FR-GPU-144). The pass reads only QUEUED
jobs and writes only through the claim, so SCHEDULED and RUNNING jobs are never
touched (FR-GPU-124).
"""

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

import models as orm
from logic.scheduler.claim import ClaimOutcome, ClaimStatus, claim, utc_now
from logic.scheduler.classes import owners_with_queued_work, resolve_class
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import JobState, JobView, NodeView, PriorityClass, QueueReason
from logic.scheduler.eligibility import ineligibility_reason, is_eligible, usable_units
from logic.scheduler.loading import load_nodes, load_queued_jobs, load_usage
from logic.scheduler.scoring import NO_USAGE, ScoreBreakdown, rank_key, score_job

log = logging.getLogger("prayog.scheduler")


class Trigger(StrEnum):
    """What started a pass (FR-GPU-121)."""
    SUBMIT = "submit"
    EDIT = "edit"
    CANCEL = "cancel"
    RUN_END = "run_end"
    NODE_STATE = "node_state"
    MAINTENANCE = "maintenance"
    TICK = "tick"


@dataclass(frozen=True)
class PassResult:
    pass_id: str
    trigger: str
    queued: int                                   # QUEUED jobs in the snapshot
    pairs_per_class: dict[PriorityClass, int]
    placed: dict[int, ClaimOutcome]               # job id -> its successful claim
    reasons: dict[int, QueueReason]               # job id -> why it is still QUEUED


def run_pass(
    sessions: sessionmaker,
    trigger: str,
    now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> PassResult:
    now = now or utc_now()
    started = time.perf_counter()
    with sessions() as session:
        jobs = load_queued_jobs(session)
        nodes = load_nodes(session, now)
        usage, total_decayed = load_usage(session, now, cfg)
        session.rollback()                        # end the read; every claim is its own transaction

        breakdowns = {j.job_id: score_job(j, now, usage.get(j.lab_id, NO_USAGE), total_decayed, cfg) for j in jobs}
        result = _Pass(session, str(uuid4()), str(trigger), now, jobs, nodes, breakdowns).run()
        _record_reasons(session, result.reasons)

    _log(result, time.perf_counter() - started)
    return result


class _Pass:
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


def _record_reasons(session: Session, reasons: dict[int, QueueReason]) -> None:
    """Write each reason only if the job is still QUEUED (another pass may have placed it since)."""
    for job_id, reason in reasons.items():
        session.execute(
            update(orm.Job)
            .where(orm.Job.id == job_id, orm.Job.state == JobState.QUEUED)
            .values(state_reason=reason)
            .execution_options(synchronize_session=False)
        )
    session.commit()


def _log(result: PassResult, seconds: float) -> None:
    """One structured record per pass (A-74). Job metadata only."""
    log.info("scheduling pass %s", json.dumps({
        "pass_id": result.pass_id,
        "trigger": result.trigger,
        "duration_ms": round(seconds * 1000, 1),
        "queued": result.queued,
        "pairs_per_class": {cls.name: n for cls, n in result.pairs_per_class.items()},
        "placed": len(result.placed),
        "reasons": dict(Counter(str(r) for r in result.reasons.values())),
    }))
