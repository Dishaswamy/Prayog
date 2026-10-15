"""
Seed labs and users for a GPU scheduling demo. Safe to run more than once.

Run from the backend folder:
    python -m scripts.seed_gpu_demo "DemoPassword123"

Creates the labs (NLP, Robotics, the institute's central owner for teaching
hardware, and a CS455 course sponsorship group), an admin, two researchers and a
student, and gives each non-admin user a lab and account type in
scheduling_contexts. That table stands in until the authorization record carries
lab and account type (PRAYOG-27, 31); it is the only part that needs no API today.

Nodes are not seeded: register them as the admin (POST /admin/nodes) and start a
simulated agent per node with the enrolment token, so the demo shows onboarding.
"""

import sys

from sqlalchemy import select

from database import SessionLocal
from logic.auth import create_user_record
from models import Lab, SchedulingContext, User

LABS = [("NLP", False), ("Robotics", False), ("Central", True), ("CS455 course group", False)]
USERS = [
    # email, role, lab, account type, course GPU-hour cap
    ("gpuadmin@iitk.ac.in", "ADMIN", None, None, None),
    ("nlp.researcher@iitk.ac.in", "RESEARCHER", "NLP", "research", None),
    ("robotics.researcher@iitk.ac.in", "RESEARCHER", "Robotics", "research", None),
    ("cs455.student@iitk.ac.in", "STUDENT", "CS455 course group", "course", 20),
]


def seed(password: str) -> None:
    db = SessionLocal()
    try:
        labs = {}
        for name, central in LABS:
            lab = db.scalar(select(Lab).where(Lab.name == name))
            if lab is None:
                lab = Lab(name=name, is_central_owner=central)
                db.add(lab)
                db.flush()
            labs[name] = lab.id

        for email, role, lab, account_type, cap in USERS:
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                user = create_user_record(db, email, password, role)
            if lab is not None and db.get(SchedulingContext, user.id) is None:
                db.add(SchedulingContext(user_id=user.id, lab_id=labs[lab], account_type=account_type,
                                         course_cap_gpu_hours=cap))
            print(f"{role:10s} {email:34s} id={user.id}" + (f"  lab={lab} ({account_type})" if lab else ""))
        db.commit()
        print("Labs:", ", ".join(f"{name}={lab_id}" for name, lab_id in labs.items()))
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python -m scripts.seed_gpu_demo "<password for every demo user>"')
        sys.exit(1)
    seed(sys.argv[1])
