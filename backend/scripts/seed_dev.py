"""
A small development world, built through the platform's own flows where they exist:

    cd backend
    python -m scripts.seed_dev [--password PASSWORD]

  admin@iitk.ac.in              ADMIN
  prof@iitk.ac.in               PROFESSOR (supervises alice and bob)
  alice@iitk.ac.in              STUDENT, lab vision, access approved by prof and admin
  bob@iitk.ac.in                RESEARCHER, lab nlp, access approved by prof and admin
  labs vision, nlp; nodes n1 (vision) and n2 (nlp), 4 GPUs each, shared and AVAILABLE

Labs, nodes and scheduling contexts have no API yet (GPU admin endpoints are pending),
so they are inserted directly. Idempotent: existing rows are left as they are.
Development only: never run against a production database.
"""

import argparse
from datetime import timedelta

from sqlalchemy import select

import models as orm
from database import SessionLocal
from logic.access_request import create_access_request, decide_access_request
from logic.admin import create_professor
from logic.auth import create_user_record, register_user
from logic.scheduler.claim import utc_now
from logic.scheduler.domain import AccountType, DonationMode, NodeState


def _user(db, email: str) -> orm.User | None:
    return db.scalars(select(orm.User).where(orm.User.email == email)).first()


def seed(password: str) -> None:
    with SessionLocal() as db:
        if _user(db, "admin@iitk.ac.in") is None:
            create_user_record(db, "admin@iitk.ac.in", password, "ADMIN")
            db.commit()
        admin = _user(db, "admin@iitk.ac.in")
        if _user(db, "prof@iitk.ac.in") is None:
            create_professor(db, "Prof. Demo", "prof@iitk.ac.in", "CSE", password)
        prof = _user(db, "prof@iitk.ac.in")

        labs = {}
        for name in ("vision", "nlp"):
            lab = db.scalars(select(orm.Lab).where(orm.Lab.name == name)).first()
            if lab is None:
                lab = orm.Lab(name=name)
                db.add(lab)
                db.flush()
            labs[name] = lab
        for node_name, lab in (("n1", "vision"), ("n2", "nlp")):
            if db.scalars(select(orm.Node).where(orm.Node.name == node_name)).first() is None:
                node = orm.Node(name=node_name, owner_lab_id=labs[lab].id, donation_mode=DonationMode.SHARED,
                                state=NodeState.AVAILABLE, unit_count=4,
                                last_heartbeat_at=utc_now())
                db.add(node)
                db.flush()
                db.add_all(orm.GpuUnit(node_id=node.id, unit_index=i) for i in range(4))
        db.commit()

        people = (
            ("alice@iitk.ac.in", "STUDENT", "Alice", "vision", "21001"),
            ("bob@iitk.ac.in", "RESEARCHER", "Bob", "nlp", "21002"),
        )
        today = utc_now().replace(microsecond=0)
        for email, role, name, lab, roll in people:
            user = _user(db, email) or register_user(db, email, password, role)
            if db.get(orm.SchedulingContext, user.id) is None:
                request = create_access_request(
                    db, user, name=name, role=role, course="BTech", branch="CSE",
                    professor_email="prof@iitk.ac.in", roll_number=roll,
                    start_date=today - timedelta(days=1),
                    end_date=today + timedelta(days=180) if role == "STUDENT" else None,
                )
                decide_access_request(db, request.id, prof, "APPROVED")
                decide_access_request(db, request.id, admin, "APPROVED")
                db.add(orm.SchedulingContext(user_id=user.id, lab_id=labs[lab].id,
                                             account_type=AccountType.RESEARCH))
                db.commit()
    print("Seeded: admin, prof, alice (vision), bob (nlp); nodes n1, n2 with 4 GPUs each.")
    print("All accounts use the password given with --password.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.seed_dev")
    parser.add_argument("--password", default="prayog-dev-password")
    seed(parser.parse_args().password)


if __name__ == "__main__":
    main()
