"""
Concurrency tests (Deliverable 3): many threads, each with its own database session,
hit the same rows at once. They need the migrated PostgreSQL test database, because
the guarantees under test are PostgreSQL row locks and unique indexes.

    C1  the same client request ID submitted 16 times at once creates one job
    C2  40 submissions at once by one user never exceed the active-job limit
    C3  8 scheduling passes at once never place two jobs on one GPU unit
    C4  25 wrong passwords at once lock the account exactly once
    C5  a cancel racing 4 scheduling passes leaves the job CANCELLED with no live allocation
"""
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from sqlalchemy import func, select

import models as orm
from database import SessionLocal
from logic.auth import MAX_FAILED_LOGINS, authenticate_user, password_hash
from logic.errors import AppError
from logic.scheduler.cancel import cancel_job
from logic.scheduler.config import DEFAULT_CONFIG
from logic.scheduler.domain import JobState
from logic.scheduler.passes import Trigger, run_pass
from logic.scheduler.requests import submit_job
from tests.scheduler_pg import SchedulerPostgresCase, requires_postgres


def _together(n: int, fn):
    """Run fn(i) in n threads released at the same instant; return results or exceptions."""
    gate = threading.Barrier(n)

    def call(i):
        gate.wait()
        try:
            return fn(i)
        except Exception as exc:          # collected, asserted on by the caller
            return exc

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(call, range(n)))


@requires_postgres
class ConcurrencyTests(SchedulerPostgresCase):
    def test_c1_duplicate_submissions_create_one_job(self):
        user = self.make_user("c1")
        self.make_node("c1", units=4)
        uid = user.id                      # read once: ORM objects never cross threads

        def submit(_):
            with SessionLocal() as db:
                return submit_job(db, uid, 1, "00:30", f"dup-{self.suffix}").job.id

        results = _together(16, submit)
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(errors, [], errors[:1])
        self.assertEqual(len(set(results)), 1, "every caller must get the same request ID")
        count = self.db.scalar(select(func.count()).select_from(orm.Job).where(orm.Job.user_id == user.id))
        self.assertEqual(count, 1)

    def test_c2_active_job_limit_holds_under_parallel_submission(self):
        user = self.make_user("c2")
        self.make_node("c2", units=4)
        cfg = replace(DEFAULT_CONFIG, max_active_jobs_per_user=5)
        uid = user.id

        def submit(i):
            with SessionLocal() as db:
                return submit_job(db, uid, 1, "00:30", f"cap-{self.suffix}-{i}", cfg=cfg).job.id

        results = _together(40, submit)
        created = [r for r in results if not isinstance(r, Exception)]
        unexpected = [r for r in results if isinstance(r, Exception) and not isinstance(r, AppError)]
        self.assertEqual(unexpected, [], unexpected[:1])
        self.assertEqual(len(created), 5)
        active = self.db.scalar(select(func.count()).select_from(orm.Job).where(
            orm.Job.user_id == user.id, orm.Job.state.in_([JobState.QUEUED, JobState.SCHEDULED, JobState.RUNNING])))
        self.assertEqual(active, 5)

    def test_c3_parallel_passes_never_double_allocate_a_unit(self):
        user = self.make_user("c3")
        node = self.make_node("c3", units=4)
        jobs = [self.make_job(user, gpus=1 + (i % 2)) for i in range(10)]

        results = _together(8, lambda _: run_pass(SessionLocal, Trigger.TICK))
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])

        self.db.expire_all()
        live = self.db.execute(
            select(orm.Allocation.id, orm.Allocation.job_id, orm.AllocationUnit.unit_id)
            .join(orm.AllocationUnit, orm.AllocationUnit.allocation_id == orm.Allocation.id)
            .where(orm.Allocation.node_id == node.id, orm.Allocation.released_at.is_(None))
        ).all()
        per_unit = Counter(unit for _, _, unit in live)
        self.assertTrue(live, "the passes should have placed something on the free node")
        self.assertEqual(max(per_unit.values()), 1, "a unit is in two live allocations")
        self.assertLessEqual(len(per_unit), 4)
        per_job = Counter(job for _, job, _ in {(a, j, 0) for a, j, _ in live})
        self.assertEqual(max(per_job.values()), 1, "a job has two live allocations")
        for unit in self.units_of(node):
            holders = {a for a, _, u in live if u == unit.id}
            self.assertEqual({unit.current_allocation_id} - {None}, holders)
        # Every SCHEDULED job of this test holds exactly one live allocation. On a shared test
        # database the passes may also place these jobs on other tests' shared nodes, so look
        # at live allocations on any node, not only this one.
        mine = {j.id for j in jobs}
        scheduled = set(self.db.scalars(select(orm.Job.id).where(orm.Job.id.in_(mine), orm.Job.state == JobState.SCHEDULED)))
        held = Counter(self.db.scalars(select(orm.Allocation.job_id).where(
            orm.Allocation.job_id.in_(mine), orm.Allocation.released_at.is_(None))))
        self.assertEqual(set(held), scheduled)
        self.assertTrue(all(n == 1 for n in held.values()), f"a job has two live allocations: {held}")

    def test_c4_parallel_wrong_passwords_lock_the_account_once(self):
        user = self.make_user("c4")
        user.password_hash = password_hash.hash("right-password-c4")
        self.db.commit()
        attempts = MAX_FAILED_LOGINS + 15
        email = user.email

        def wrong(_):
            with SessionLocal() as db:
                return authenticate_user(db, email, "wrong-password")

        self.assertTrue(all(r is None for r in _together(attempts, wrong)))
        self.db.expire_all()
        fresh = self.db.get(orm.User, user.id)
        self.assertIsNotNone(fresh.locked_until, "the account must be locked")
        events = Counter(self.db.execute(
            select(orm.AuditEvent.operation, orm.AuditEvent.reason)
            .where(orm.AuditEvent.resource_ids.any(f"user:{user.id}"))
        ).all())
        self.assertEqual(events[("auth.lockout", None)], 1)
        self.assertEqual(events[("auth.login", "failed credentials")], MAX_FAILED_LOGINS)
        self.assertEqual(events[("auth.login", "locked out")], attempts - MAX_FAILED_LOGINS)
        with SessionLocal() as db:
            self.assertIsNone(authenticate_user(db, user.email, "right-password-c4"))

    def test_c5_cancel_racing_scheduling_passes(self):
        user = self.make_user("c5")
        node = self.make_node("c5", units=2)
        job = self.make_job(user, gpus=2)
        job_id, uid = job.id, user.id

        def act(i):
            if i == 0:
                with SessionLocal() as db:
                    return cancel_job(db, job_id, uid)
            return run_pass(SessionLocal, Trigger.TICK)

        results = _together(5, act)
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.db.expire_all()
        state = self.db.get(orm.Job, job.id).state
        live = self.db.scalars(select(orm.Allocation.id).where(
            orm.Allocation.job_id == job.id, orm.Allocation.released_at.is_(None))).all()
        # Either the cancel won (CANCELLED, nothing held) or a pass placed it first and the
        # cancel asked the node to stop it (the allocation is released when the agent confirms).
        if state == JobState.CANCELLED:
            self.assertEqual(live, [])
            mine = set(self.db.scalars(select(orm.Allocation.id).where(orm.Allocation.job_id == job_id)))
            # Other queued jobs may take the freed units; none may still point at this job.
            self.assertFalse({u.current_allocation_id for u in self.units_of(node)} & mine)
        else:
            self.assertIn(state, (JobState.SCHEDULED, JobState.RUNNING))
            self.assertEqual(len(live), 1)
            alloc = self.db.get(orm.Allocation, live[0])
            self.assertIsNotNone(alloc.stop_reason, "the cancel must have asked the node to stop the job")

    def test_c6_one_inconsistent_unit_does_not_stop_scheduling(self):
        # Found while running C3 on a shared test database: a unit whose live allocation row
        # disagrees with its current_allocation_id made the backstop index fire, and the
        # IntegrityError escaped run_pass, so every later pass died at the same claim and
        # nothing anywhere was scheduled. The pass must skip that claim and go on.
        # Both nodes are reserved to this test's lab, so other tests' queued jobs stay off them.
        user = self.make_user("c6")
        bad = self.make_node("c6-bad", units=1, donation_mode="reserved")
        good = self.make_node("c6-good", units=1, donation_mode="reserved")
        holder = self.make_job(user, gpus=1)
        self.make_running(holder, bad)
        unit = self.units_of(bad)[0]
        unit.current_allocation_id = None          # the disagreement: allocation row live, unit says free
        self.db.commit()
        waiting = self.make_job(user, gpus=1)
        waiting_id = waiting.id

        run_pass(SessionLocal, Trigger.TICK)       # must not raise
        run_pass(SessionLocal, Trigger.TICK)

        self.db.expire_all()
        placed = self.db.scalars(select(orm.Allocation.node_id).where(
            orm.Allocation.job_id == waiting_id, orm.Allocation.released_at.is_(None))).all()
        self.assertEqual(placed, [good.id], "the job should land on the healthy node")
