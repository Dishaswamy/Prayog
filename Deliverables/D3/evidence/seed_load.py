"""Seed the load-test DB: seed_dev's world, then 100 jobs for alice (50 cancelled, 50 queued)."""
from sqlalchemy import select
import models as orm
from database import SessionLocal
from scripts.seed_dev import seed
from logic.gpu.scheduler.requests import submit_job
from logic.gpu.scheduler.cancel import cancel_job

seed()
with SessionLocal() as db:
    alice = db.scalars(select(orm.User).where(orm.User.email == "alice@iitk.ac.in")).one()
    n = db.scalar(select(orm.Job.id).where(orm.Job.user_id == alice.id).limit(1))
    if n is None:
        ids = []
        for i in range(100):
            ids.append(submit_job(db, alice.id, 1, "00:30", f"load-{i}").job.id)
            if i < 50:
                cancel_job(db, ids[-1], alice.id)
        db.commit()
    print("alice jobs:", db.scalar(select(orm.func.count()).select_from(orm.Job).where(orm.Job.user_id == alice.id)),
          "queued:", db.scalar(select(orm.func.count()).select_from(orm.Job).where(orm.Job.user_id == alice.id, orm.Job.state == "QUEUED")))
