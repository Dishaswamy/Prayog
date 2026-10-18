"""PostgreSQL fixtures for the GPU scheduling admin tests.

The scheduler relies on row locks, partial unique indexes and ARRAY columns, so these
tests run against the migrated PostgreSQL test database (as in CI) and are skipped
otherwise. The logic under test commits, so every row carries a per-test suffix
instead of being rolled back.
"""
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import any_, select

import models as orm
from database import SessionLocal
from logic.dependencies import get_current_user
from logic.scheduler.domain import AccountType, JobState, NodeState

requires_postgres = unittest.skipUnless(
    os.getenv("PRAYOG_RUN_SCHEDULER_POSTGRES_TESTS") == "1",
    "requires the dedicated migrated PostgreSQL test database",
)


class SchedulerPostgresCase(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.suffix = uuid4().hex[:10]
        self.now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)   # the database stores naive UTC
        self.lab = self.make_lab("lab")

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    # ── rows ──────────────────────────────────────────────────────────────

    def make_lab(self, label: str) -> orm.Lab:
        lab = orm.Lab(name=f"{label}-{self.suffix}")
        self.db.add(lab)
        self.db.commit()
        return lab

    def make_user(
        self, label: str, role: str = "RESEARCHER", lab: orm.Lab | None = None, access: bool = True
    ) -> orm.User:
        user = orm.User(
            user_id=f"{label}-{self.suffix}"[:50],
            email=f"{label}-{self.suffix}@example.test",
            password_hash="not-a-real-password-hash",
            role=role,
        )
        self.db.add(user)
        self.db.flush()
        if role != "ADMIN":
            self.db.add(orm.SchedulingContext(
                user_id=user.id, lab_id=(lab or self.lab).id, account_type=AccountType.RESEARCH,
            ))
            self.db.flush()
            if access and role in ("STUDENT", "RESEARCHER"):
                self.approve_access(user, role)
        self.db.commit()
        return user

    def approve_access(self, user: orm.User, role: str, purpose: str = "RESEARCH_PROJECT") -> orm.AccessRequest:
        """An APPROVED access request, which job submission requires."""
        professor = self.db.scalars(select(orm.Professor).where(orm.Professor.email == f"prof-{self.suffix}@example.test")).first()
        if professor is None:
            prof_user = orm.User(
                user_id=f"prof-{self.suffix}"[:50], email=f"prof-{self.suffix}@example.test",
                password_hash="not-a-real-password-hash", role="PROFESSOR",
            )
            self.db.add(prof_user)
            self.db.flush()
            professor = orm.Professor(user_id=prof_user.id, name="Prof", email=prof_user.email, department="CSE")
            self.db.add(professor)
            self.db.flush()
        request = orm.AccessRequest(
            user_id=user.id, name=user.user_id, role=role, course="BTech", branch="CSE", purpose=purpose,
            professor_id=professor.id, roll_number=f"r-{user.user_id}"[:50], start_date=self.now - timedelta(days=1),
            end_date=self.now + timedelta(days=30) if role == "STUDENT" else None,
            status_professor="APPROVED", status_admin="APPROVED", final_status="APPROVED",
        )
        self.db.add(request)
        return request

    def make_node(
        self,
        label: str = "node",
        units: int = 2,
        donation_mode: str = "shared",
        teaching_pool: bool = False,
        state: NodeState = NodeState.AVAILABLE,
    ) -> orm.Node:
        node = orm.Node(
            name=f"{label}-{self.suffix}", owner_lab_id=self.lab.id, donation_mode=donation_mode,
            teaching_pool=teaching_pool, state=state, unit_count=units, state_changed_at=self.now,
            last_heartbeat_at=self.now, reported_unit_count=units,
        )
        self.db.add(node)
        self.db.flush()
        self.db.add_all(orm.GpuUnit(node_id=node.id, unit_index=i) for i in range(units))
        self.db.commit()
        return node

    def units_of(self, node: orm.Node) -> list[orm.GpuUnit]:
        return list(self.db.query(orm.GpuUnit).filter_by(node_id=node.id).order_by(orm.GpuUnit.unit_index))

    def make_job(
        self,
        user: orm.User,
        state: JobState = JobState.QUEUED,
        gpus: int = 1,
        project_ref: str | None = "secret-project",
        snapshot_hash: str | None = "sha256:secret-snapshot",
    ) -> orm.Job:
        job = orm.Job(
            user_id=user.id, project_ref=project_ref, snapshot_hash=snapshot_hash, lab_id=self.lab.id,
            account_type=AccountType.RESEARCH, gpus=gpus, walltime_minutes=60, state=state,
            client_request_id=uuid4().hex, submitted_at=self.now, effective_submit_at=self.now,
        )
        self.db.add(job)
        self.db.commit()
        return job

    def make_running(self, job: orm.Job, node: orm.Node, started_minutes_ago: int = 10) -> orm.Allocation:
        """Place a job on the node's first free units and mark it RUNNING, as the agent's start report would."""
        started = self.now - timedelta(minutes=started_minutes_ago)
        free = [u for u in self.units_of(node) if u.current_allocation_id is None][: job.gpus]
        allocation = orm.Allocation(job_id=job.id, node_id=node.id, claimed_at=started, acknowledged_at=started)
        self.db.add(allocation)
        self.db.flush()
        for unit in free:
            self.db.add(orm.AllocationUnit(allocation_id=allocation.id, unit_id=unit.id))
            unit.current_allocation_id = allocation.id
        self.db.add(orm.Run(job_id=job.id, allocation_id=allocation.id, node_id=node.id, started_at=started))
        job.state = JobState.RUNNING
        self.db.commit()
        return allocation

    def audit_rows(self, operation: str, resource: str) -> list[orm.AuditEvent]:
        self.db.expire_all()
        return list(
            self.db.query(orm.AuditEvent)
            .filter(orm.AuditEvent.operation == operation, resource == any_(orm.AuditEvent.resource_ids))
            .order_by(orm.AuditEvent.id)
        )

    # ── API ───────────────────────────────────────────────────────────────

    def client_as(self, user: orm.User) -> TestClient:
        """The app with `user` signed in. Triggered scheduling passes are recorded, not run,
        so a test never places another test's jobs."""
        from main import app

        app.dependency_overrides[get_current_user] = lambda: user
        self.addCleanup(app.dependency_overrides.pop, get_current_user, None)
        self.passes = []
        runner = patch("routers.scheduler.admin.run_pass", lambda *a, **k: self.passes.append(k))
        runner.start()
        self.addCleanup(runner.stop)
        return TestClient(app)
