"""
GET /jobs loads the page in a batch (Deliverable 3 performance fix). It used to call
job_status once per job, 3 to 6 queries each plus a full reload of the lab's nodes for
every queued job, so a page of 100 jobs took hundreds of queries.

These tests pin the two promises of the batch: every job is reported exactly as
job_status reports it on its own, and the number of queries does not grow with the
number of jobs.
"""
from dataclasses import fields
from datetime import timedelta

from sqlalchemy import event

import models as orm
from database import engine
from logic.gpu.scheduler.domain import JobState
from logic.gpu.scheduler.requests import job_status, job_statuses
from helpers.scheduler_pg import SchedulerPostgresCase, requires_postgres


def _same(a, b) -> dict:
    """Fields where two JobStatus values differ (rows compared by id)."""
    diff = {}
    for f in fields(a):
        x, y = getattr(a, f.name), getattr(b, f.name)
        if hasattr(x, "id") or hasattr(y, "id"):
            x, y = getattr(x, "id", None), getattr(y, "id", None)
        if x != y:
            diff[f.name] = (x, y)
    return diff


class _Count:
    def __init__(self):
        self.n = 0

    def __call__(self, *args):
        self.n += 1


@requires_postgres
class BatchedJobListTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("batch")
        self.node = self.make_node("batch-a", units=2)
        other = self.make_node("batch-b", units=1)

        self.running = self.make_job(self.user, gpus=1)
        self.running.walltime_minutes = 240
        alloc = self.make_running(self.running, self.node)
        unit = self.units_of(self.node)[0]
        # Inside the running job's walltime (a planned stop), after a queued job's 60 minutes.
        self.db.add(orm.MaintenanceWindow(unit_id=unit.id, starts_at=self.now + timedelta(minutes=90),
                                          ends_at=self.now + timedelta(hours=2), reason="fan swap"))
        alloc.stop_reason = None

        self.scheduled = self.make_job(self.user, gpus=1)
        sched = self.make_running(self.scheduled, self.node)
        sched.acknowledged_at = None
        self.scheduled.state = JobState.SCHEDULED

        self.ended = self.make_job(self.user, gpus=1)
        done = self.make_running(self.ended, other)
        done.released_at, done.release_reason = self.now, "COMPLETED"
        for u in self.units_of(other):
            u.current_allocation_id = None
        run = self.db.query(orm.Run).filter_by(allocation_id=done.id).one()
        run.ended_at, run.end_state, run.exit_code = self.now, "COMPLETED", 0
        self.ended.state = JobState.COMPLETED

        full = self.make_node("batch-c", units=2)          # both units busy, no maintenance: p0_free_at
        self.big = self.make_job(self.user, gpus=2)
        self.make_running(self.big, full)

        self.cancelled = self.make_job(self.user, state=JobState.CANCELLED)
        self.queued = [self.make_job(self.user, gpus=2) for _ in range(3)]   # node full: p0_free_at is set
        self.db.commit()

    def test_every_job_matches_the_single_job_view(self):
        batch = job_statuses(self.db, self.user.id, 100, now=self.now)
        self.assertEqual([s.job.id for s in batch], sorted([s.job.id for s in batch], reverse=True))
        self.assertEqual(len(batch), 8)
        for s in batch:
            alone = job_status(self.db, self.user.id, s.job.id, now=self.now)
            self.assertEqual(_same(s, alone), {}, f"job {s.job.id} ({s.job.state})")
        by_id = {s.job.id: s for s in batch}
        self.assertIsNotNone(by_id[self.queued[0].id].p0_free_at)
        self.assertEqual(by_id[self.running.id].planned_stop_reason, "fan swap")
        self.assertIsNone(by_id[self.scheduled.id].walltime_ends_at)
        self.assertEqual(by_id[self.ended.id].run.exit_code, 0)

    def test_limit_keeps_the_newest(self):
        batch = job_statuses(self.db, self.user.id, 2, now=self.now)
        self.assertEqual([s.job.id for s in batch], [self.queued[2].id, self.queued[1].id])

    def test_query_count_does_not_grow_with_jobs(self):
        def queries() -> int:
            self.db.expire_all()
            count = _Count()
            event.listen(engine, "before_cursor_execute", count)
            try:
                job_statuses(self.db, self.user.id, 500, now=self.now)
            finally:
                event.remove(engine, "before_cursor_execute", count)
            return count.n

        before = queries()
        for _ in range(30):
            self.make_job(self.user, gpus=2)
        self.make_job(self.user, state=JobState.CANCELLED)
        self.assertEqual(queries(), before)
