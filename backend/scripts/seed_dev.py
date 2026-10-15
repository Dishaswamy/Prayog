"""
A small development world for the Steward, built through the platform's own flows:

    cd backend
    python -m scripts.seed_dev [--password PASSWORD]

  admin@iitk.ac.in              ADMIN
  prof@iitk.ac.in               PROFESSOR (supervises alice and bob)
  alice@iitk.ac.in              STUDENT, lab vision, access approved by prof and admin
  bob@iitk.ac.in                RESEARCHER, lab nlp, access approved by prof and admin
  labs vision, nlp; nodes n1 (vision) and n2 (nlp), 4 GPUs each, shared

Nodes are registered as an admin would (PENDING_AGENT); the script prints each new
node's one-time enrolment token and the command that starts its simulated agent,
whose first heartbeat makes the node AVAILABLE. Labs and scheduling contexts have no
API yet, so they are inserted directly. Idempotent: existing rows are left as they
are (a node registered earlier prints no token). Development only.
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
from logic.scheduler.domain import AccountType, DonationMode
from logic.scheduler.nodes import register_node

NODES = (("n1", "vision"), ("n2", "nlp"))
UNITS = 4


def _user(db, email: str) -> orm.User | None:
    return db.scalars(select(orm.User).where(orm.User.email == email)).first()


def seed(password: str) -> dict[str, str]:
    """Returns {node name: enrolment token} for the nodes registered by this call."""
    tokens: dict[str, str] = {}
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
        db.commit()
        for node_name, lab in NODES:
            if db.scalars(select(orm.Node.id).where(orm.Node.name == node_name)).first() is None:
                registration = register_node(db, node_name, labs[lab].id, DonationMode.SHARED,
                                             teaching_pool=False, unit_count=UNITS)
                tokens[node_name] = registration.enrolment_token

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
    return tokens


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.seed_dev")
    parser.add_argument("--password", default="prayog-dev-password")
    tokens = seed(parser.parse_args().password)
    print("Seeded: admin, prof, alice (vision), bob (nlp); nodes n1, n2 with 4 GPUs each.")
    print("All accounts use the password given with --password.")
    for node, token in tokens.items():
        print(f"Start node {node}'s agent (add --exit-code 1 --log-file run.log to simulate a failing job):")
        print(f"  python -m node_agent.simulated --units {UNITS} --state-dir .agent-{node} "
              f"--enrolment-token {token}")


if __name__ == "__main__":
    main()
