"""
From an approved access request to a node: the user never names a lab or a GPU. The
server derives the lab from the professor (research) or the central lab (course) and
the scheduler places the job from that.
"""
from datetime import timedelta

from decimal import Decimal

from sqlalchemy import update

import models as orm
from database import SessionLocal
from logic.access_request import create_access_request, decide_access_request
from logic.scheduler.claim import ClaimStatus, claim
from logic.scheduler.domain import AccountType, JobState, PriorityClass
from logic.scheduler.scoring import ScoreBreakdown
from logic.scheduler.passes import Trigger, run_pass
from logic.scheduler.requests import RejectCode, SubmissionRejected, submit_job
from tests.scheduler_pg import SchedulerPostgresCase, requires_postgres


@requires_postgres
class LabSchedulingTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        # The test database keeps rows between runs; leftover queued jobs must not compete
        self.db.query(orm.Job).filter(orm.Job.state == JobState.QUEUED).update({orm.Job.state: JobState.CANCELLED})
        self.lab_b = self.make_lab("labB")
        self.central = orm.Lab(name=f"central-{self.suffix}", is_central_owner=True)
        self.db.add(self.central)
        self.admin = self.make_user("admin", "ADMIN")
        self.db.commit()

    def _professor(self, label, lab):
        user = self.make_user(label, "PROFESSOR")
        self.db.query(orm.SchedulingContext).filter_by(user_id=user.id).delete()
        prof = orm.Professor(user_id=user.id, name=label, email=user.email, department="CSE", lab_id=lab.id)
        self.db.add(prof)
        self.db.commit()
        return user, prof

    def _approved_user(self, label, role, purpose, professor):
        prof_user, prof = professor
        user = self.make_user(label, role, access=False)
        self.db.query(orm.SchedulingContext).filter_by(user_id=user.id).delete()
        self.db.commit()
        request = create_access_request(
            self.db, user, name=label, role=role, course="BTech", branch="CSE", purpose=purpose,
            professor_email=prof.email, roll_number=f"r-{label}-{self.suffix}", start_date=self.now,
            end_date=self.now + timedelta(days=60) if role == "STUDENT" else None,
        )
        decide_access_request(self.db, request.id, prof_user, "APPROVED")
        decide_access_request(self.db, request.id, self.admin, "APPROVED")
        return user

    def _run(self, user):
        """Submit one GPU request exactly as the API does, run a pass, return (job, node)."""
        job = submit_job(self.db, user.id, 1, "01:00", f"req-{user.id}-{self.suffix}").job
        run_pass(SessionLocal, Trigger.SUBMIT)
        self.db.expire_all()
        job = self.db.get(orm.Job, job.id)
        allocation = self.db.query(orm.Allocation).filter_by(job_id=job.id).first()
        return job, (self.db.get(orm.Node, allocation.node_id) if allocation else None)

    def test_researcher_runs_on_the_professors_own_lab_node(self):
        node_a = self.make_node("a-reserved", donation_mode="reserved", owner=self.lab)
        self.make_node("b-reserved", donation_mode="reserved", owner=self.lab_b)
        user = self._approved_user("res-a", "RESEARCHER", None, self._professor("prof-a", self.lab))

        job, node = self._run(user)
        self.assertEqual(job.lab_id, self.lab.id)
        self.assertEqual(job.account_type, AccountType.RESEARCH)
        self.assertEqual((job.state, node.id), (JobState.SCHEDULED, node_a.id))

    def test_a_researcher_under_another_professor_runs_on_that_labs_node(self):
        self.make_node("a-reserved", donation_mode="reserved", owner=self.lab)
        node_b = self.make_node("b-reserved", donation_mode="reserved", owner=self.lab_b)
        user = self._approved_user("res-b", "RESEARCHER", None, self._professor("prof-b", self.lab_b))

        job, node = self._run(user)
        self.assertEqual(job.lab_id, self.lab_b.id)
        self.assertEqual(node.id, node_b.id)

    def test_research_student_works_in_the_lab_like_a_researcher(self):
        node_a = self.make_node("a-reserved", donation_mode="reserved", owner=self.lab)
        user = self._approved_user("stu-res", "STUDENT", "RESEARCH_PROJECT", self._professor("prof-s", self.lab))

        job, node = self._run(user)
        self.assertEqual((job.account_type, job.lab_id, node.id), (AccountType.RESEARCH, self.lab.id, node_a.id))

    def test_course_student_runs_only_on_a_teaching_pool_node(self):
        self.make_node("a-reserved", donation_mode="reserved", owner=self.lab)       # never for a course job
        self.make_node("teach", teaching_pool=True, owner=self.central)
        user = self._approved_user("stu-course", "STUDENT", "COURSE_PROJECT", self._professor("prof-c", self.lab))

        job, node = self._run(user)
        self.assertEqual(job.account_type, AccountType.COURSE)
        self.assertTrue(self.db.get(orm.Lab, job.lab_id).is_central_owner)
        self.assertEqual(job.state, JobState.SCHEDULED)
        self.assertTrue(node.teaching_pool)

    def test_research_job_never_runs_on_another_labs_reserved_or_the_teaching_pool(self):
        self.make_node("b-reserved", donation_mode="reserved", owner=self.lab_b)
        self.make_node("teach", teaching_pool=True, owner=self.central)
        # lab A has no node of its own; any placement must be on someone's SHARED, non-teaching node
        user = self._approved_user("res-lonely", "RESEARCHER", None, self._professor("prof-l", self.lab))

        try:
            job, node = self._run(user)
        except SubmissionRejected as rejected:     # up front: no node anywhere is reachable
            self.assertEqual(rejected.code, RejectCode.GPUS_EXCEED_MAX_NODE)
            return
        if node is not None:
            self.assertEqual(node.donation_mode, "shared")
            self.assertFalse(node.teaching_pool)


    # ── the lab drives the scheduling score ───────────────────────────────

    def _contest(self, lab_a_usage_hours):
        """
        One free GPU on a shared node owned by a third lab. Lab A's research student
        submits first, then lab B's researcher. Lab A is charged `lab_a_usage_hours`.
        Returns (job_a, job_b), after one pass.
        """
        host = self.make_lab("host")
        node = self.make_node("contested", units=1, donation_mode="shared", owner=host)
        # leftover nodes from other tests must not offer a second place
        self.db.execute(update(orm.Node).where(orm.Node.id != node.id).values(state="DOWN"))
        self.db.commit()

        student_a = self._approved_user("stu-a", "STUDENT", "RESEARCH_PROJECT", self._professor("prof-a", self.lab))
        researcher_b = self._approved_user("res-b", "RESEARCHER", None, self._professor("prof-b", self.lab_b))

        if lab_a_usage_hours:
            finished = self.make_job(student_a, state=JobState.COMPLETED)
            self.db.add(orm.UsageEntry(
                job_id=finished.id, consumer_lab_id=self.lab.id, provider_lab_id=host.id,
                gpu_seconds=lab_a_usage_hours * 3600, ended_at=self.now, charged=True,
            ))
            self.db.commit()

        job_a = submit_job(self.db, student_a.id, 1, "01:00", f"a-{self.suffix}").job
        job_b = submit_job(self.db, researcher_b.id, 1, "01:00", f"b-{self.suffix}").job
        self.assertEqual((job_a.lab_id, job_b.lab_id), (self.lab.id, self.lab_b.id))

        run_pass(SessionLocal, Trigger.SUBMIT)
        self.db.expire_all()
        return self.db.get(orm.Job, job_a.id), self.db.get(orm.Job, job_b.id)

    def test_without_any_usage_the_earlier_job_wins(self):
        job_a, job_b = self._contest(lab_a_usage_hours=0)
        self.assertEqual((job_a.state, job_b.state), (JobState.SCHEDULED, JobState.QUEUED))

    def test_a_lab_that_has_used_a_lot_ranks_below_a_fresh_lab(self):
        """Same age, same request: lab A's heavy past usage lowers its fair share, so lab B goes first,
        even though A's job is older. The student counts as lab A, exactly like a researcher in A."""
        job_a, job_b = self._contest(lab_a_usage_hours=100)
        self.assertEqual((job_a.state, job_b.state), (JobState.QUEUED, JobState.SCHEDULED))
        decision = self.db.query(orm.SchedulingDecision).filter_by(job_id=job_b.id).one()
        self.assertAlmostEqual(decision.fair_share, 1.0)      # lab B has used nothing
        self.assertIsNotNone(decision.contribution)           # research jobs are scored on F and C


    # ── the claim re-checks how the node is shared ────────────────────────

    def _claim(self, job, node):
        breakdown = ScoreBreakdown(age=0.0, fair_share=0.5, contribution=0.5, size=0.1, score=Decimal("0.1"), escalated=False)
        with SessionLocal() as session:
            return claim(session, job.id, node.id, PriorityClass.P1, breakdown, "pass-x", "submit")

    def _queued_research_job(self):
        user = self._approved_user("res-claim", "RESEARCHER", None, self._professor("prof-claim", self.lab))
        return submit_job(self.db, user.id, 1, "01:00", f"claim-{self.suffix}").job

    def test_claim_works_on_a_node_that_is_still_shared(self):
        node = self.make_node("guest-ok", donation_mode="shared", owner=self.lab_b)
        job = self._queued_research_job()
        self.assertEqual(self._claim(job, node).status, ClaimStatus.CLAIMED)

    def test_claim_refuses_a_node_made_reserved_after_the_snapshot(self):
        node = self.make_node("guest-late", donation_mode="shared", owner=self.lab_b)
        job = self._queued_research_job()
        # the admin flips the node after the pass chose it
        self.db.execute(update(orm.Node).where(orm.Node.id == node.id).values(donation_mode="reserved"))
        self.db.commit()

        self.assertEqual(self._claim(job, node).status, ClaimStatus.NODE_UNAVAILABLE)
        self.db.expire_all()
        self.assertEqual(self.db.get(orm.Job, job.id).state, JobState.QUEUED)
        self.assertIsNone(self.db.query(orm.Allocation).filter_by(job_id=job.id).first())

    def test_claim_refuses_a_node_moved_into_the_teaching_pool_for_a_research_job(self):
        node = self.make_node("pool-late", donation_mode="shared", owner=self.lab_b)
        job = self._queued_research_job()
        self.db.execute(update(orm.Node).where(orm.Node.id == node.id).values(teaching_pool=True))
        self.db.commit()
        self.assertEqual(self._claim(job, node).status, ClaimStatus.NODE_UNAVAILABLE)
