"""PRAYOG-81: a Platform Admin cancels any user's job with a recorded reason (US-ADM-07; FR-GPU-065, 099)."""
from logic.errors import BadRequestError, ForbiddenError
from logic.scheduler.cancel import admin_cancel_job, cancel_job
from logic.scheduler.domain import JobState, ReleaseReason

from scheduler_pg import SchedulerPostgresCase, requires_postgres


@requires_postgres
class AdminCancelJobTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", role="ADMIN")
        self.owner = self.make_user("owner")
        self.other = self.make_user("other")

    def test_admin_cancels_a_queued_job_and_the_reason_actor_and_time_are_audited(self):
        job = self.make_job(self.owner)

        result = admin_cancel_job(self.db, job.id, self.admin.id, "  Blocking the pool for a demo  ", now=self.now)

        self.assertEqual(result.state, JobState.CANCELLED)
        self.assertFalse(result.stop_pending)
        self.db.refresh(job)
        self.assertEqual(job.state, JobState.CANCELLED)
        self.assertEqual(job.cancel_reason, "admin: Blocking the pool for a demo")
        [event] = self.audit_rows("job.cancel_by_admin", f"job:{job.id}")
        self.assertEqual(event.actor, str(self.admin.id))
        self.assertEqual(event.actor_kind, "user")
        self.assertEqual(event.at, self.now)
        self.assertEqual(event.reason, "Blocking the pool for a demo")
        self.assertIn(f"owner:{self.owner.id}", event.resource_ids)

    def test_running_job_is_asked_to_stop_and_keeps_its_units_until_the_agent_confirms(self):
        node = self.make_node()
        job = self.make_job(self.owner)
        allocation = self.make_running(job, node)

        result = admin_cancel_job(self.db, job.id, self.admin.id, "Suspected runaway job", now=self.now)

        self.assertEqual(result.state, JobState.RUNNING)
        self.assertTrue(result.stop_pending)
        self.db.refresh(allocation)
        self.assertEqual(allocation.stop_reason, ReleaseReason.CANCELLED)
        self.assertIsNone(allocation.released_at)
        self.assertEqual(len(self.audit_rows("job.cancel_by_admin", f"job:{job.id}")), 1)

    def test_reason_is_mandatory_and_nothing_changes_without_one(self):
        job = self.make_job(self.owner)
        for reason in (None, "", "   "):
            with self.subTest(reason=reason), self.assertRaises(BadRequestError):
                admin_cancel_job(self.db, job.id, self.admin.id, reason)
        self.db.refresh(job)
        self.assertEqual(job.state, JobState.QUEUED)
        self.assertEqual(self.audit_rows("job.cancel_by_admin", f"job:{job.id}"), [])

    def test_another_user_cannot_cancel_the_job_through_the_owner_path(self):
        job = self.make_job(self.owner)
        with self.assertRaises(ForbiddenError):
            cancel_job(self.db, job.id, self.other.id)
        self.db.refresh(job)
        self.assertEqual(job.state, JobState.QUEUED)

    def test_route_cancels_for_an_admin_and_refuses_everyone_else(self):
        job = self.make_job(self.owner)

        refused = self.client_as(self.other).post(f"/admin/jobs/{job.id}/cancel", json={"reason": "mine now"})
        self.assertEqual(refused.status_code, 403)
        owner_refused = self.client_as(self.owner).post(f"/admin/jobs/{job.id}/cancel", json={"reason": "x"})
        self.assertEqual(owner_refused.status_code, 403)
        missing = self.client_as(self.admin).post(f"/admin/jobs/{job.id}/cancel", json={})
        self.assertEqual(missing.status_code, 422)
        self.db.refresh(job)
        self.assertEqual(job.state, JobState.QUEUED)

        done = self.client_as(self.admin).post(f"/admin/jobs/{job.id}/cancel", json={"reason": "Quota abuse"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["state"], JobState.CANCELLED)
        self.assertEqual(len(self.passes), 1)

    def test_finished_or_unknown_jobs_are_refused(self):
        job = self.make_job(self.owner, state=JobState.COMPLETED)
        client = self.client_as(self.admin)
        self.assertEqual(client.post(f"/admin/jobs/{job.id}/cancel", json={"reason": "late"}).status_code, 409)
        self.assertEqual(client.post("/admin/jobs/999999999/cancel", json={"reason": "none"}).status_code, 404)
        self.assertEqual(self.audit_rows("job.cancel_by_admin", f"job:{job.id}"), [])
