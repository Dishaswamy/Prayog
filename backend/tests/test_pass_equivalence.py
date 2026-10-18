"""
The scheduling pass must place jobs exactly as the original algorithm did. The original
(tests/reference_pass.py) re-scanned every job against every node in each of the four
class rounds; the pass now files each eligible pair under its class once. Over many
random pools and queues, both must make the same claims in the same order and report
the same results. No database: the claim step is replaced by an in-memory stand-in that
also injects the failures a real claim can have.
"""
import random
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from logic.scheduler.claim import ClaimOutcome, ClaimStatus
from logic.scheduler.domain import (
    AccountType, DonationMode, JobView, MaintenanceWindow, NodeState, NodeView, UnitView,
)
from logic.scheduler.eligibility import usable_units
from logic.scheduler.passes import _Pass
from logic.scheduler.scoring import NO_USAGE, LabUsage, score_job
from tests.reference_pass import ReferencePass

NOW = datetime(2026, 10, 19, 12, 0)


def random_world(seed: int):
    rng = random.Random(seed)
    labs = rng.randint(1, 8)
    nodes = []
    for i in range(rng.randint(1, 25)):
        teaching = rng.random() < 0.2
        mode = DonationMode.SHARED if teaching or rng.random() < 0.5 else DonationMode.RESERVED
        state = rng.choices(list(NodeState), weights=[1, 12, 2, 2, 1, 1])[0]
        units = []
        for u in range(rng.choice([1, 2, 4, 8])):
            windows = ()
            if rng.random() < 0.15:
                start = NOW + timedelta(minutes=rng.randint(-30, 300))
                windows = (MaintenanceWindow(start, start + timedelta(minutes=rng.randint(10, 200))),)
            units.append(UnitView(i * 100 + u, u, free=rng.random() < 0.5, maintenance=windows))
        nodes.append(NodeView(i, rng.randrange(labs), mode, teaching, state, tuple(units)))
    jobs = [
        JobView(
            j, lab_id=rng.randrange(labs),
            account_type=rng.choice([AccountType.RESEARCH] * 3 + [AccountType.COURSE]),
            gpus=rng.choice([1, 1, 2, 3, 4, 8]), walltime_minutes=rng.choice([30, 60, 240, 720]),
            submit_time=NOW - timedelta(minutes=rng.randint(0, 600)),     # some older than T_max: escalated
        )
        for j in range(rng.randint(0, 60))
    ]
    usage = {lab: LabUsage(rng.random() * 500, rng.random() * 100, rng.random() * 100) for lab in range(labs)}
    total = sum(u.decayed_gpu_hours for u in usage.values())
    breakdowns = {j.job_id: score_job(j, NOW, usage.get(j.lab_id, NO_USAGE), total) for j in jobs}
    return jobs, nodes, breakdowns


class FakeClaims:
    """Stands in for the database claim. Deterministic, so both passes meet the same failures."""

    def __init__(self, jobs, nodes):
        self.jobs = {j.job_id: j for j in jobs}
        self.nodes = {n.node_id: n for n in nodes}
        self.taken: set[int] = set()
        self.calls: list[tuple] = []

    def __call__(self, session, job_id, node_id, cls, breakdown, pass_id, trigger, now=None):
        job, node = self.jobs[job_id], self.nodes[node_id]
        if job_id % 11 == 0:
            status = ClaimStatus.JOB_UNAVAILABLE            # another pass took the job
        elif node_id % 13 == 0:
            status = ClaimStatus.NODE_UNAVAILABLE           # the node left AVAILABLE meanwhile
        elif (job_id * 31 + node_id) % 7 == 0:
            status = ClaimStatus.LOST_CLAIM_RACE            # another pass won the units
        else:
            view = replace(node, units=tuple(replace(u, free=u.free and u.unit_id not in self.taken) for u in node.units))
            usable = usable_units(job, view, now)
            if len(usable) < job.gpus:
                status = ClaimStatus.LOST_CLAIM_RACE
            else:
                unit_ids = tuple(u.unit_id for u in usable[: job.gpus])
                self.taken.update(unit_ids)
                self.calls.append((job_id, node_id, int(cls), ClaimStatus.CLAIMED))
                return ClaimOutcome(ClaimStatus.CLAIMED, allocation_id=len(self.calls), unit_ids=unit_ids)
        self.calls.append((job_id, node_id, int(cls), status))
        return ClaimOutcome(status)


def run(pass_cls, patch_target, jobs, nodes, breakdowns):
    fake = FakeClaims(jobs, nodes)
    with patch(patch_target, fake):
        result = pass_cls(None, "pass", "tick", NOW, list(jobs), list(nodes), breakdowns).run()
    return fake.calls, result


class PassEquivalenceTests(unittest.TestCase):
    def test_same_claims_and_results_over_many_random_pools(self):
        placed_somewhere = failed_somewhere = 0
        for seed in range(400):
            jobs, nodes, breakdowns = random_world(seed)
            old_calls, old = run(ReferencePass, "tests.reference_pass.claim", jobs, nodes, breakdowns)
            new_calls, new = run(_Pass, "logic.scheduler.passes.claim", jobs, nodes, breakdowns)

            with self.subTest(seed=seed):
                self.assertEqual(new_calls, old_calls, "same claims in the same order")
                self.assertEqual(new.placed, old.placed)
                self.assertEqual(new.reasons, old.reasons)
                self.assertEqual(new.pairs_per_class, old.pairs_per_class)
                self.assertEqual((new.failed_claims, new.queued), (old.failed_claims, old.queued))
            placed_somewhere += bool(old.placed)
            failed_somewhere += old.failed_claims > 0

        # the scenarios must be rich enough to mean something
        self.assertGreater(placed_somewhere, 200)
        self.assertGreater(failed_somewhere, 100)

    def test_every_class_is_exercised(self):
        classes = set()
        for seed in range(400):
            jobs, nodes, breakdowns = random_world(seed)
            calls, _ = run(_Pass, "logic.scheduler.passes.claim", jobs, nodes, breakdowns)
            classes |= {cls for _, _, cls, status in calls if status == ClaimStatus.CLAIMED}
        self.assertEqual(classes, {0, 1, 2, 3})


if __name__ == "__main__":
    unittest.main()
