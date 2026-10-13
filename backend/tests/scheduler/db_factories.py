"""Builders for scheduling rows: the database twin of factories.py."""

from datetime import timedelta
from itertools import count

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import User
from scheduler import models as orm
from scheduler.domain import AccountType, DonationMode, JobState, NodeState
from tests.scheduler.factories import NOW


class Seed:
    """Adds rows through one session. Call commit() before other sessions need to see them."""

    def __init__(self, session: Session):
        self.session = session
        self._serial = count(1)
        self._users: dict[int, int] = {}       # lab id -> a user in that lab

    def _add(self, row):
        self.session.add(row)
        self.session.flush()
        return row

    def commit(self) -> None:
        self.session.commit()

    def lab(self, name: str, central: bool = False) -> int:
        return self._add(orm.Lab(name=name, is_central_owner=central)).id

    def _user_in(self, lab_id: int) -> int:
        if lab_id not in self._users:
            n = next(self._serial)
            user = User(user_id=f"user{n}", email=f"user{n}@iitk.ac.in", password_hash="-", role="RESEARCHER")
            self._users[lab_id] = self._add(user).id
        return self._users[lab_id]

    def node(
        self,
        owner: int,
        units: int = 4,
        shared: bool = True,
        teaching: bool = False,
        state: NodeState = NodeState.AVAILABLE,
    ) -> int:
        node = self._add(orm.Node(
            name=f"node-{next(self._serial)}",
            owner_lab_id=owner,
            donation_mode=DonationMode.SHARED if shared else DonationMode.RESERVED,
            teaching_pool=teaching,
            state=state,
            unit_count=units,
        ))
        self.session.add_all(orm.GpuUnit(node_id=node.id, unit_index=i) for i in range(units))
        self.session.flush()
        return node.id

    def unit_ids(self, node_id: int) -> list[int]:
        return list(self.session.scalars(
            text("SELECT id FROM gpu_units WHERE node_id = :n ORDER BY unit_index"), {"n": node_id}
        ))

    def job(
        self,
        lab: int,
        gpus: int = 2,
        walltime_h: float = 4,
        waited_min: float = 0,
        course: bool = False,
        state: JobState = JobState.QUEUED,
    ) -> int:
        submitted = NOW - timedelta(minutes=waited_min)
        return self._add(orm.Job(
            user_id=self._user_in(lab),
            lab_id=lab,
            account_type=AccountType.COURSE if course else AccountType.RESEARCH,
            gpus=gpus,
            walltime_minutes=int(walltime_h * 60),
            state=state,
            client_request_id=f"req-{next(self._serial)}",
            submitted_at=submitted,
            effective_submit_at=submitted,
        )).id

    def window(self, unit_id: int, start_in_h: float, length_h: float = 2) -> None:
        start = NOW + timedelta(hours=start_in_h)
        self._add(orm.MaintenanceWindow(
            unit_id=unit_id, starts_at=start, ends_at=start + timedelta(hours=length_h), reason="test"
        ))


# Each query returns the rows that break the invariant; all must be empty.
INVARIANT_QUERIES = {
    "I1: a unit with two live allocations": """
        SELECT unit_id FROM allocation_units WHERE released_at IS NULL GROUP BY unit_id HAVING count(*) > 1
    """,
    "I2: a job with two live allocations": """
        SELECT job_id FROM allocations WHERE released_at IS NULL GROUP BY job_id HAVING count(*) > 1
    """,
    "claim marker disagrees with the live allocation": """
        SELECT u.id FROM gpu_units u
        LEFT JOIN allocation_units au ON au.unit_id = u.id AND au.released_at IS NULL
        WHERE u.current_allocation_id IS DISTINCT FROM au.allocation_id
    """,
    "SCHEDULED without a live allocation, or the reverse": """
        SELECT j.id FROM jobs j
        LEFT JOIN allocations a ON a.job_id = j.id AND a.released_at IS NULL
        WHERE (j.state = 'SCHEDULED') <> (a.id IS NOT NULL)
    """,
    "an allocation spanning two nodes (A-80)": """
        SELECT a.id FROM allocations a
        JOIN allocation_units au ON au.allocation_id = a.id
        JOIN gpu_units u ON u.id = au.unit_id
        WHERE u.node_id <> a.node_id
    """,
}


def broken_invariants(session: Session) -> dict[str, list]:
    broken = {}
    for name, sql in INVARIANT_QUERIES.items():
        rows = session.execute(text(sql)).scalars().all()
        if rows:
            broken[name] = rows
    return broken
