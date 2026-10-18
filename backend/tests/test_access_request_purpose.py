"""Access-request purpose decides the GPU scheduling context created on approval."""
import unittest
from datetime import timedelta

import models as orm
from logic.access_request import (
    create_access_request, decide_access_request, revoke_access, sweep_access_revocations,
)
from logic.errors import BadRequestError, ConflictError
from logic.scheduler.domain import AccountType
from logic.scheduler.domain import JobState
from logic.scheduler.requests import RejectCode, SubmissionRejected, edit_job, job_status, submit_job
from tests.scheduler_pg import SchedulerPostgresCase, requires_postgres


@requires_postgres
class AccessRequestPurposeTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.central = orm.Lab(name=f"central-{self.suffix}", is_central_owner=True)
        self.db.add(self.central)
        self.admin = self.make_user("admin", "ADMIN")
        self.prof_user = self.make_user("prof", "PROFESSOR")
        self.professor = orm.Professor(
            user_id=self.prof_user.id, name="Prof", email=self.prof_user.email,
            department="CSE", lab_id=self.lab.id,
        )
        self.db.add(self.professor)
        self.db.commit()

    def _request(self, role, purpose, label):
        user = self.make_user(label, role, access=False)
        self.db.query(orm.SchedulingContext).filter_by(user_id=user.id).delete()
        self.db.commit()
        start = self.now
        return user, create_access_request(
            self.db, user, name=label, role=role, course="BTech", branch="CSE",
            professor_email=self.professor.email, roll_number=f"r-{label}-{self.suffix}",
            start_date=start, end_date=start + timedelta(days=60) if role == "STUDENT" else None,
            purpose=purpose,
        )

    def _approve(self, request):
        decide_access_request(self.db, request.id, self.prof_user, "APPROVED")
        return decide_access_request(self.db, request.id, self.admin, "APPROVED")

    def test_researcher_defaults_to_research_project(self):
        _, request = self._request("RESEARCHER", None, "res")
        self.assertEqual(request.purpose, "RESEARCH_PROJECT")

    def test_researcher_cannot_ask_for_course_project(self):
        with self.assertRaises(BadRequestError):
            self._request("RESEARCHER", "COURSE_PROJECT", "res2")

    def test_student_must_choose_a_purpose(self):
        with self.assertRaises(BadRequestError):
            self._request("STUDENT", None, "stu")

    def test_course_student_gets_course_account_in_central_lab(self):
        user, request = self._request("STUDENT", "COURSE_PROJECT", "course")
        self._approve(request)
        ctx = self.db.get(orm.SchedulingContext, user.id)
        self.assertEqual(ctx.account_type, AccountType.COURSE)
        self.assertTrue(self.db.get(orm.Lab, ctx.lab_id).is_central_owner)

    def test_research_student_gets_research_account_in_professors_lab(self):
        user, request = self._request("STUDENT", "RESEARCH_PROJECT", "lab")
        self._approve(request)
        ctx = self.db.get(orm.SchedulingContext, user.id)
        self.assertEqual((ctx.account_type, ctx.lab_id), (AccountType.RESEARCH, self.lab.id))

    def test_no_context_until_both_approve(self):
        user, request = self._request("STUDENT", "RESEARCH_PROJECT", "pending")
        decide_access_request(self.db, request.id, self.prof_user, "APPROVED")
        self.assertIsNone(self.db.get(orm.SchedulingContext, user.id))

    def test_research_approval_needs_a_professor_lab(self):
        self.professor.lab_id = None
        self.db.commit()
        _, request = self._request("STUDENT", "RESEARCH_PROJECT", "nolab")
        decide_access_request(self.db, request.id, self.prof_user, "APPROVED")
        with self.assertRaises(ConflictError):
            decide_access_request(self.db, request.id, self.admin, "APPROVED")

    def test_revoke_removes_gpu_access(self):
        user, request = self._request("STUDENT", "COURSE_PROJECT", "revoke")
        self._approve(request)
        revoke_access(self.db, request.id, self.admin)
        self.assertIsNone(self.db.get(orm.SchedulingContext, user.id))

    # ── job submission reads the approved purpose ─────────────────────────

    def _submit(self, user):
        return submit_job(self.db, user.id, 1, "01:00", f"job-{user.id}-{self.suffix}")

    def test_course_purpose_submits_a_course_job(self):
        self.make_node("teach", teaching_pool=True)
        user, request = self._request("STUDENT", "COURSE_PROJECT", "cjob")
        self._approve(request)
        self.assertEqual(self._submit(user).job.account_type, AccountType.COURSE)

    def test_research_purpose_submits_a_research_job_in_the_professors_lab(self):
        self.make_node("labnode")
        user, request = self._request("STUDENT", "RESEARCH_PROJECT", "rjob")
        self._approve(request)
        job = self._submit(user).job
        self.assertEqual((job.account_type, job.lab_id), (AccountType.RESEARCH, self.lab.id))

    def test_context_that_disagrees_with_purpose_is_refused(self):
        user, request = self._request("STUDENT", "COURSE_PROJECT", "drift")
        self._approve(request)
        self.db.get(orm.SchedulingContext, user.id).account_type = AccountType.RESEARCH
        self.db.commit()
        with self.assertRaises(SubmissionRejected) as caught:
            self._submit(user)
        self.assertEqual(caught.exception.code, RejectCode.AUTHORIZATION_INACTIVE)

    def test_no_approved_request_means_no_job(self):
        user = self.make_user("noapproval", access=False)
        with self.assertRaises(SubmissionRejected) as caught:
            self._submit(user)
        self.assertEqual(caught.exception.code, RejectCode.AUTHORIZATION_INACTIVE)

    def test_student_past_end_date_cannot_submit(self):
        user, request = self._request("STUDENT", "RESEARCH_PROJECT", "expired")
        self._approve(request)
        request.start_date = self.now - timedelta(days=10)
        request.end_date = self.now - timedelta(days=1)
        self.db.commit()
        with self.assertRaises(SubmissionRejected):
            self._submit(user)

    # ── automatic revoke of expired students ──────────────────────────────

    def test_expired_student_is_revoked_automatically(self):
        user, request = self._request("STUDENT", "COURSE_PROJECT", "exp")
        self._approve(request)
        revoked = sweep_access_revocations(self.db, now=request.end_date + timedelta(seconds=1))
        self.assertGreaterEqual(revoked, 1)
        self.db.refresh(request)
        self.assertEqual((request.final_status, request.revoke_reason), ("REVOKED", "EXPIRED"))
        self.assertIsNotNone(request.revoked_at)
        self.assertIsNone(self.db.get(orm.SchedulingContext, user.id))
        self.assertIsNone(self.db.query(orm.Student).filter_by(user_id=user.id).first())

    def test_student_before_end_date_is_kept(self):
        user, request = self._request("STUDENT", "COURSE_PROJECT", "live")
        self._approve(request)
        sweep_access_revocations(self.db, now=request.end_date - timedelta(days=1))
        self.db.refresh(request)
        self.assertEqual(request.final_status, "APPROVED")
        self.assertIsNotNone(self.db.get(orm.SchedulingContext, user.id))

    def test_researcher_is_never_auto_revoked(self):
        user, request = self._request("RESEARCHER", None, "resv")
        self._approve(request)
        sweep_access_revocations(self.db, now=self.now + timedelta(days=3650))
        self.db.refresh(request)
        self.assertEqual(request.final_status, "APPROVED")

    def test_admin_revoke_records_reason(self):
        _, request = self._request("RESEARCHER", None, "adm")
        self._approve(request)
        revoke_access(self.db, request.id, self.admin)
        self.assertEqual(request.revoke_reason, "ADMIN")

    # ── unfinished jobs delay the revoke until they finish ────────────────

    def _active_student(self, label):
        self.make_node(f"n-{label}")
        user, request = self._request("STUDENT", "RESEARCH_PROJECT", label)
        self._approve(request)
        return user, request, self._submit(user).job

    def test_revoke_with_unfinished_job_is_pending_not_immediate(self):
        user, request, job = self._active_student("pend")
        revoke_access(self.db, request.id, self.admin)

        self.db.refresh(request)
        self.assertEqual(request.final_status, "APPROVED")             # not revoked yet
        self.assertIsNotNone(request.revoke_requested_at)
        self.assertEqual(request.revoke_reason, "ADMIN")
        self.db.refresh(job)
        self.assertEqual(job.state, JobState.QUEUED)                   # the job is left alone
        self.assertEqual(job_status(self.db, user.id, job.id).job.id, job.id)   # results stay readable

    def test_pending_revoke_blocks_new_and_edited_work(self):
        user, request, job = self._active_student("block")
        revoke_access(self.db, request.id, self.admin)
        with self.assertRaises(SubmissionRejected) as caught:
            submit_job(self.db, user.id, 1, "01:00", f"another-{self.suffix}")
        self.assertEqual(caught.exception.code, RejectCode.AUTHORIZATION_INACTIVE)
        with self.assertRaises(SubmissionRejected):
            edit_job(self.db, user.id, job.id, gpus=1)

    def test_sweep_waits_while_the_job_is_unfinished(self):
        user, request, job = self._active_student("wait")
        revoke_access(self.db, request.id, self.admin)
        sweep_access_revocations(self.db)     # other users' leftovers may be revoked; only this one matters
        self.db.refresh(request)
        self.assertEqual(request.final_status, "APPROVED")
        self.assertIsNotNone(self.db.get(orm.SchedulingContext, user.id))

    def test_pending_revoke_completes_when_the_job_finishes(self):
        user, request, job = self._active_student("done")
        revoke_access(self.db, request.id, self.admin)
        job.state = JobState.COMPLETED
        self.db.commit()
        self.assertGreaterEqual(sweep_access_revocations(self.db), 1)
        self.db.refresh(request)
        self.assertEqual((request.final_status, request.revoke_reason), ("REVOKED", "ADMIN"))
        self.assertIsNotNone(request.revoked_at)
        self.assertIsNone(self.db.get(orm.SchedulingContext, user.id))
        self.assertEqual(job_status(self.db, user.id, job.id).job.state, JobState.COMPLETED)   # results still readable

    def test_expired_student_with_unfinished_job_is_revoked_after_it_finishes(self):
        user, request, job = self._active_student("expjob")
        later = request.end_date + timedelta(days=1)
        sweep_access_revocations(self.db, now=later)
        self.db.refresh(request)
        self.assertEqual((request.final_status, request.revoke_reason), ("APPROVED", "EXPIRED"))
        job.state = JobState.COMPLETED
        self.db.commit()
        sweep_access_revocations(self.db, now=later)
        self.db.refresh(request)
        self.assertEqual((request.final_status, request.revoke_reason), ("REVOKED", "EXPIRED"))

if __name__ == "__main__":
    unittest.main()
