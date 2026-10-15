"""PRAYOG-91: submit-by-snapshot through the ordinary scheduling path (§14.3; ADR-08).

A rebuild is validated, classed, ranked and claimed like any job, carrying
reproduces:<run_id>, with no priority advantage.
"""
from types import SimpleNamespace
from unittest.mock import patch

import models as orm
from logic.rebuild import RebuildRejected, start_rebuild
from logic.scheduler.classes import owners_with_queued_work, resolve_class
from logic.scheduler.domain import JobState
from logic.scheduler.loading import load_node_view, to_job_view
from logic.scheduler.requests import SubmissionRejected, submit_job

from scheduler_pg import SchedulerPostgresCase, requires_postgres


@requires_postgres
class RebuildSchedulingPathTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("researcher")
        self.node = self.make_node(units=2)
        self.ledger = {}
        lookup = patch("logic.rebuild.rebuildable_snapshot",
                       lambda db, snapshot_id, user_id: self.ledger.get((snapshot_id, user_id)))
        lookup.start()
        self.addCleanup(lookup.stop)

    def _recorded(self, snapshot_id="11111111-2222-3333-4444-555555555555", run_id=42, owner=None):
        self.ledger[(snapshot_id, (owner or self.user).id)] = SimpleNamespace(project_id="proj-1", run_id=run_id)
        return snapshot_id

    def _rebuild(self, snapshot_id, gpus=1, user=None, request_id=None):
        status, created = start_rebuild(self.db, (user or self.user).id, snapshot_id, gpus, "01:00",
                                        request_id or f"rb-{self.suffix}-{snapshot_id[:8]}-{gpus}")
        return status, created

    def test_a_recorded_snapshot_is_queued_as_an_ordinary_job_carrying_reproduces(self):
        status, created = self._rebuild(self._recorded(run_id=42))

        self.assertTrue(created)
        job = status.job
        self.assertEqual(job.reproduces, "reproduces:42")
        self.assertEqual(job.state, JobState.QUEUED)
        self.assertEqual(job.project_ref, "proj-1")
        self.assertEqual(job.effective_submit_at, job.submitted_at)      # a fresh submit time, no carried age
        self.assertEqual(status.state, "REBUILD_INITIATED")

    def test_no_priority_advantage_over_a_first_time_job(self):
        rebuild, _ = self._rebuild(self._recorded())
        first_time = submit_job(self.db, self.user.id, 1, "01:00", f"plain-{self.suffix}").job

        node = load_node_view(self.db, self.node, self.now)
        views = [to_job_view(rebuild.job), to_job_view(first_time)]
        owners = owners_with_queued_work(views)
        self.assertEqual(resolve_class(views[0], node, owners), resolve_class(views[1], node, owners))
        for field in ("lab_id", "account_type", "gpus", "walltime_minutes", "state"):
            self.assertEqual(getattr(rebuild.job, field), getattr(first_time, field))
        self.assertEqual(views[0].submit_time, rebuild.job.submitted_at)   # ranked by its own submit time

    def test_rebuilds_are_validated_like_any_job(self):
        snapshot = self._recorded()
        with self.assertRaises(SubmissionRejected):
            self._rebuild(snapshot, gpus=999)                            # no node has 999 GPUs
        with self.assertRaises(SubmissionRejected):
            self._rebuild(snapshot, gpus=0)
        self.assertEqual(self.db.query(orm.Job).filter_by(user_id=self.user.id).count(), 0)

    def test_retry_with_the_same_request_id_returns_the_same_attempt(self):
        snapshot = self._recorded()
        first, created = self._rebuild(snapshot, request_id=f"retry-{self.suffix}")
        again, created_again = self._rebuild(snapshot, request_id=f"retry-{self.suffix}")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.attempt.id, again.attempt.id)

    def test_unknown_or_foreign_snapshots_are_rejected_without_a_job_or_attempt(self):
        other = self.make_user("other")
        foreign = self._recorded("99999999-0000-0000-0000-000000000000", owner=other)
        for snapshot_id in ("00000000-dead-beef-0000-000000000000", foreign):
            with self.subTest(snapshot_id=snapshot_id), self.assertRaises(RebuildRejected):
                self._rebuild(snapshot_id)
        self.assertEqual(self.db.query(orm.Job).filter_by(user_id=self.user.id).count(), 0)
        self.assertEqual(self.db.query(orm.RebuildAttempt).filter_by(user_id=self.user.id).count(), 0)

    def test_a_snapshot_one_of_your_jobs_ran_names_that_jobs_run(self):
        original = self.make_job(self.user, state=JobState.COMPLETED, snapshot_hash=f"sha256:{self.suffix}")
        allocation = self.make_running(original, self.node)
        original.state = JobState.COMPLETED
        for unit in self.units_of(self.node):
            unit.current_allocation_id = None
        self.db.commit()
        run_id = self.db.query(orm.Run.id).filter_by(allocation_id=allocation.id).scalar()

        status, _ = self._rebuild(f"sha256:{self.suffix}")
        self.assertEqual(status.job.reproduces, f"reproduces:{run_id}")

        stranger = self.make_user("stranger")
        with self.assertRaises(RebuildRejected):
            self._rebuild(f"sha256:{self.suffix}", user=stranger)

    def test_route_reports_reproduces(self):
        snapshot = self._recorded(run_id=7)
        client = self.client_as(self.user)
        with patch("routers.rebuilds.run_pass") as run_pass:
            response = client.post("/rebuilds", json={"snapshot_hash": snapshot, "gpus": 1, "walltime": "00:30",
                                                      "client_request_id": f"route-{self.suffix}"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["reproduces"], "reproduces:7")
        self.assertEqual(response.json()["state"], "REBUILD_INITIATED")
        run_pass.assert_called_once()

        rejected = client.post("/rebuilds", json={"snapshot_hash": "not-mine", "gpus": 1, "walltime": "00:30",
                                                  "client_request_id": f"route2-{self.suffix}"})
        self.assertEqual(rejected.status_code, 404)
        self.assertEqual(rejected.json()["code"], "REBUILD_REJECTED")
