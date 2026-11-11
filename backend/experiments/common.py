"""Helpers shared by the Deliverable 3 experiments. Run them against a throwaway deployment, never real data."""

import json
import math
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

import mlflow_models  # noqa: F401  (registers the ledger tables on the shared metadata)
import models as orm
from database import SessionLocal
from logic.auth.authentication import create_access_token
from logic.gpu.scheduler.claim import utc_now
from logic.gpu.scheduler.domain import AccountType, DonationMode, JobState, NodeState

BACKEND = Path(__file__).resolve().parents[1]
RUN_TAG = uuid4().hex[:8]


def percentile(values, q):
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q / 100 * len(ordered)) - 1)]


def summary_ms(seconds):
    """Latency summary of a list of durations in seconds, in milliseconds."""
    ms = [s * 1000 for s in seconds]
    if not ms:
        return {"n": 0}
    return {
        "n": len(ms),
        "mean": round(sum(ms) / len(ms), 2),
        "p50": round(percentile(ms, 50), 2),
        "p95": round(percentile(ms, 95), 2),
        "p99": round(percentile(ms, 99), 2),
        "max": round(max(ms), 2),
    }


def write_results(path, results):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"results written to {path}", file=sys.stderr)


class World:
    """Rows for an experiment, committed so that the API and the tick can see them."""

    def __init__(self):
        self.db = SessionLocal()
        self.serial = 0
        self.professor = None

    def _next(self):
        self.serial += 1
        return f"{RUN_TAG}-{self.serial}"

    def lab(self, central=False):
        lab = orm.Lab(name=f"lab-{self._next()}", is_central_owner=central)
        self.db.add(lab)
        self.db.commit()
        return lab.id

    def _ensure_professor(self):
        if self.professor is None:
            tag = self._next()
            user = orm.User(user_id=f"prof-{tag}", email=f"prof-{tag}@example.test", password_hash="-", role="PROFESSOR")
            self.db.add(user)
            self.db.flush()
            prof = orm.Professor(user_id=user.id, name="Prof", email=user.email, department="CSE")
            self.db.add(prof)
            self.db.flush()
            self.professor = prof.id
        return self.professor

    def user(self, lab_id, course=False, cap=None, role=None):
        """A user with an approved access request, and a real token from the login code."""
        tag = self._next()
        role = role or ("STUDENT" if course else "RESEARCHER")
        user = orm.User(user_id=f"u-{tag}", email=f"u-{tag}@example.test", password_hash="-", role=role)
        self.db.add(user)
        self.db.flush()
        if role != "ADMIN":
            now = utc_now()
            self.db.add(orm.SchedulingContext(
                user_id=user.id, lab_id=lab_id, account_type=AccountType.COURSE if course else AccountType.RESEARCH,
                course_cap_gpu_hours=cap,
            ))
            self.db.add(orm.AccessRequest(
                user_id=user.id, name=user.user_id, role=role, course="BTech", branch="CSE",
                purpose="COURSE_PROJECT" if course else "RESEARCH_PROJECT", professor_id=self._ensure_professor(),
                roll_number=f"r-{tag}", start_date=now - timedelta(days=1),
                end_date=now + timedelta(days=30) if course else None,
                status_professor="APPROVED", status_admin="APPROVED", final_status="APPROVED",
            ))
        self.db.commit()
        return user.id, {"Authorization": f"Bearer {create_access_token(user)}"}

    def node(self, owner_lab, units=4, shared=True, teaching=False, state=NodeState.AVAILABLE):
        node = orm.Node(
            name=f"node-{self._next()}", owner_lab_id=owner_lab,
            donation_mode=DonationMode.SHARED if shared else DonationMode.RESERVED,
            teaching_pool=teaching, state=state, unit_count=units,
        )
        self.db.add(node)
        self.db.flush()
        self.db.add_all(orm.GpuUnit(node_id=node.id, unit_index=i) for i in range(units))
        self.db.commit()
        return node.id

    def queued_job(self, user_id, lab_id, gpus=1, minutes=60, account_type=AccountType.RESEARCH, age_minutes=0):
        now = utc_now()
        job = orm.Job(
            user_id=user_id, lab_id=lab_id, account_type=account_type, gpus=gpus, walltime_minutes=minutes,
            state=JobState.QUEUED, client_request_id=f"seed-{self._next()}",
            submitted_at=now - timedelta(minutes=age_minutes), effective_submit_at=now - timedelta(minutes=age_minutes),
        )
        self.db.add(job)
        self.db.commit()
        return job.id

    def close(self):
        self.db.close()


# What must always hold (NFR-GPU-001, 002, 005). Each query returns the rows that break it.
INVARIANTS = {
    "unit_with_two_live_allocations":
        "SELECT unit_id FROM allocation_units WHERE released_at IS NULL GROUP BY unit_id HAVING count(*) > 1",
    "job_with_two_live_allocations":
        "SELECT job_id FROM allocations WHERE released_at IS NULL GROUP BY job_id HAVING count(*) > 1",
    "partial_allocation":
        "SELECT a.id FROM allocations a JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN allocation_units u ON u.allocation_id = a.id AND u.released_at IS NULL "
        "WHERE a.released_at IS NULL GROUP BY a.id, j.gpus HAVING count(u.unit_id) <> j.gpus",
    "marker_disagrees_with_allocation":
        "SELECT g.id FROM gpu_units g LEFT JOIN allocation_units u ON u.unit_id = g.id AND u.released_at IS NULL "
        "WHERE (g.current_allocation_id IS NULL) <> (u.unit_id IS NULL) "
        "OR g.current_allocation_id IS DISTINCT FROM u.allocation_id",
    "placed_job_without_allocation":
        "SELECT j.id FROM jobs j LEFT JOIN allocations a ON a.job_id = j.id AND a.released_at IS NULL "
        "WHERE j.state IN ('SCHEDULED', 'RUNNING') AND a.id IS NULL",
}


def broken_invariants(session=None):
    own = session is None
    session = session or SessionLocal()
    try:
        broken = {}
        for name, sql in INVARIANTS.items():
            rows = session.execute(text(sql)).fetchall()
            if rows:
                broken[name] = len(rows)
        return broken
    finally:
        if own:
            session.close()


def wait_until(predicate, timeout, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
