"""Behaviour the full code review fixed: sign-in lockout and its audit (FR-AUTH-60, FR-AUD-01), active
access for anything that stores data, what happens to runs around a handover or a cancel, approved Steward
fixes running real code, the Professor ledger, Steward links ending with their session, and test accounts."""
import unittest
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

import models as orm
from database import SessionLocal
from integration.steward_bridge import JwtPrincipalResolver, PlatformAuthorizer, PlatformSubmissions
from logic.auth.authentication import MAX_FAILED_LOGINS
from logic.mlflow.outbox import record_sealed_runs
from logic.mlflow.professor_ledger import list_professor_ledger
from logic.gpu.scheduler.cancel import cancel_job
from logic.gpu.scheduler.domain import ReleaseReason
from logic.gpu.scheduler.lifecycle import request_stop
from mlflow_models import ModelRun
from prayog_steward.ports import JobSpec, SubmissionRequest
from scripts.seed_dev import test_password as seeded_password
from test_cases.helpers.access_api_case import PW, AccessApiCase
from test_cases.helpers.agent_case import AgentRunCase
from test_cases.helpers.gpu_case import requires_postgres


class TestAccountTests(unittest.TestCase):
    def test_a_test_accounts_password_is_its_user_id_reversed(self):
        self.assertEqual(seeded_password("alice@iitk.ac.in"), "ecila")
        self.assertEqual(seeded_password("nlp.researcher@iitk.ac.in"), "rehcraeser.pln")


@requires_postgres
class LoginLockoutTests(AccessApiCase):
    def attempt(self, email, password, role="STUDENT"):
        return self.client.post("/auth/login", json={"email": email, "password": password, "role": role})

    def setUp(self):
        super().setUp()
        self.new_user("locked")
        self.email = f"locked-{self.suffix}@iitk.ac.in"

    def account(self):
        with SessionLocal() as db:
            return db.scalars(select(orm.User).where(orm.User.email == self.email)).one()

    def fail_until_locked(self):
        for _ in range(MAX_FAILED_LOGINS):
            self.assertEqual(self.attempt(self.email, "wrong-password").status_code, 401)

    def test_the_right_password_is_refused_during_a_lockout_with_the_same_answer(self):
        self.fail_until_locked()
        locked, unknown = self.attempt(self.email, PW), self.attempt(f"nobody-{self.suffix}@iitk.ac.in", PW)
        self.assertEqual(locked.status_code, 401)
        self.assertEqual(locked.json(), unknown.json())

    def test_the_account_opens_again_after_the_lockout_period(self):
        self.fail_until_locked()
        with SessionLocal() as db:
            user = db.scalars(select(orm.User).where(orm.User.email == self.email)).one()
            user.locked_until = self.now - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.attempt(self.email, PW).status_code, 200)

    def test_a_good_login_clears_the_count(self):
        for _ in range(MAX_FAILED_LOGINS - 1):
            self.attempt(self.email, "wrong-password")
        self.assertEqual(self.attempt(self.email, PW).status_code, 200)
        self.assertEqual(self.account().failed_logins, 0)
        self.assertEqual(self.attempt(self.email, "wrong-password").status_code, 401)
        self.assertIsNone(self.account().locked_until)

    def test_sign_ins_and_the_lockout_are_audited_without_what_was_typed(self):
        self.fail_until_locked()
        user_id = self.account().id
        with SessionLocal() as db:
            rows = db.scalars(select(orm.AuditEvent).where(orm.AuditEvent.actor == str(user_id))).all()
        self.assertEqual(sum(r.operation == "auth.lockout" for r in rows), 1)
        self.assertEqual(sum(r.operation == "auth.login" and r.outcome == "denied" for r in rows), MAX_FAILED_LOGINS)
        self.assertTrue(any(r.operation == "auth.register" for r in rows))
        self.assertFalse(any("wrong-password" in (r.reason or "") for r in rows))


@requires_postgres
class ActiveAccessTests(AccessApiCase):
    def test_an_unapproved_account_stores_nothing_but_can_still_read(self):
        auth = self.new_user("waiting")
        refused = self.client.post("/mlflow/projects", headers=auth, json={"name": "p"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "INSUFFICIENT_PERMISSION"))
        staged = self.client.put(f"/mlflow/staging/files/{'a' * 64}", headers=auth, params={"size": 1}, content=b"x")
        self.assertEqual(staged.status_code, 403)
        self.assertEqual(self.client.get("/mlflow/projects", headers=auth).json(), [])

    def test_approval_opens_it_and_revoking_closes_it(self):
        auth, request_id = self.approved_user("working")
        self.assertEqual(self.client.post("/mlflow/projects", headers=auth, json={"name": "p"}).status_code, 201)
        self.client.post(f"/access-requests/{request_id}/revoke", headers=self.admin)
        refused = self.client.post("/mlflow/projects", headers=auth, json={"name": "q"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "ACCESS_REVOKED"))


@requires_postgres
class StewardSessionTests(AccessApiCase):
    def principal(self):
        auth, _ = self.approved_user("linked")
        token = auth["Authorization"].removeprefix("Bearer ")
        return auth, JwtPrincipalResolver().resolve(token)

    def test_a_session_ends_when_it_is_logged_out(self):
        auth, principal = self.principal()
        self.assertTrue(PlatformAuthorizer().session_active(principal.session_id))
        self.client.post("/auth/logout", headers=auth)
        self.assertFalse(PlatformAuthorizer().session_active(principal.session_id))

    def test_a_session_ends_when_the_password_is_reset(self):
        _, principal = self.principal()
        with SessionLocal() as db:
            db.get(orm.User, int(principal.principal_id)).token_version += 1
            db.commit()
        self.assertFalse(PlatformAuthorizer().session_active(principal.session_id))


@requires_postgres
class RunsInFlightTests(AgentRunCase):
    def placed_run(self, **kw) -> tuple[dict, object, object]:
        run = self.submit_program(dataset=False, **kw)
        node, credential = self.make_agent_node()
        return run, self.place(run["job_id"], node), self.agent_client(credential)

    def professor_user(self) -> orm.User:
        professor = self.db.query(orm.Professor).filter(orm.Professor.email == f"prof-{self.suffix}@example.test").one()
        return self.db.get(orm.User, professor.user_id)

    def test_a_handover_leaves_a_placed_run_able_to_start_and_store_its_results(self):
        run, allocation, agent = self.placed_run()
        professor = self.professor_user()
        profile = self.db.query(orm.Professor).filter(orm.Professor.user_id == professor.id).one()
        pushed = self.client.post(f"/mlflow/projects/{self.pid}/handover", json={"professor_id": profile.id})
        self.assertEqual(pushed.status_code, 201, pushed.text)
        approved = self.client_for_other(professor).post(f"/mlflow/handovers/{pushed.json()['handover_id']}/approve")
        self.assertEqual(approved.status_code, 200, approved.text)

        self.assertEqual(agent.get(f"/agent/allocations/{allocation.id}/workload").status_code, 200)
        self.assertTrue(agent.post(f"/agent/allocations/{allocation.id}/acknowledge").json()["started"])
        stored = agent.post(f"/agent/allocations/{allocation.id}/results", params={"kind": "LOG"}, content=b"done\n")
        self.assertEqual(stored.status_code, 201, stored.text)

    def running_job(self) -> tuple[int, object]:
        run, allocation, agent = self.placed_run()
        agent.post(f"/agent/allocations/{allocation.id}/acknowledge")
        return run["job_id"], allocation

    def stop_reason(self, allocation) -> str:
        self.db.expire_all()
        return self.db.get(orm.Allocation, allocation.id).stop_reason

    def test_an_owners_cancel_replaces_a_stop_that_would_requeue_the_job(self):
        job_id, allocation = self.running_job()
        request_stop(self.db, job_id, ReleaseReason.MAINTENANCE)
        cancel_job(self.db, job_id, self.user.id)
        self.assertEqual(self.stop_reason(allocation), ReleaseReason.CANCELLED)

    def test_a_later_platform_stop_does_not_undo_a_cancel(self):
        job_id, allocation = self.running_job()
        cancel_job(self.db, job_id, self.user.id)
        request_stop(self.db, job_id, ReleaseReason.MAINTENANCE)
        self.assertEqual(self.stop_reason(allocation), ReleaseReason.CANCELLED)

    def test_an_approved_steward_fix_runs_the_same_code_again(self):
        run, allocation, agent = self.placed_run(hyperparameters={"lr": 0.5})
        agent.post(f"/agent/allocations/{allocation.id}/acknowledge")
        failed = self.db.scalars(select(orm.Run).where(orm.Run.job_id == run["job_id"])).one()
        self.db.get(ModelRun, run["run_id"]).status = "FAILED"     # the Steward only fixes a run that has failed
        self.db.commit()
        request = SubmissionRequest(
            project_id=f"u{self.user.id}:{self.pid}", parent_run_id=str(failed.id), approving_user=str(self.user.id),
            job_spec=JobSpec(command_args="", env_pins=(), gpu_count=2, walltime_minutes=90, image_digest=""),
            idempotency_key=str(uuid4()),
        )
        steward = PlatformSubmissions(trigger_pass=False)
        receipt = steward.submit(request)
        self.assertTrue(receipt.accepted, receipt.reason)
        self.db.expire_all()
        fixed = self.db.query(ModelRun).filter(ModelRun.job_id == int(receipt.job_id)).one()
        self.assertEqual(fixed.project_version_id, self.db.get(ModelRun, run["run_id"]).project_version_id)
        self.assertEqual(fixed.hyperparameters, {"lr": 0.5})
        job = self.db.get(orm.Job, int(receipt.job_id))
        self.assertEqual((job.gpus, job.walltime_minutes), (2, 90))
        self.assertEqual(steward.submit(request).job_id, receipt.job_id)       # a retried approval queues nothing new

    def test_a_sealed_run_reaches_the_professors_ledger_without_any_outside_service(self):
        run = self.submit_program()
        self.drive(self.make_agent(run), self.finished(run))
        for _ in range(20):                       # other tests' events share this database
            record_sealed_runs(self.db)
        ledger = list_professor_ledger(self.db, self.professor_user())
        self.assertIn(run["run_id"], [entry["run_id"] for entry in ledger])     # self.user is a Researcher
