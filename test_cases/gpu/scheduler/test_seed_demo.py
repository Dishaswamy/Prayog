"""The demo seed script (scripts/seed_gpu_demo.py)."""

import pytest
from sqlalchemy import func, select

import models as orm
from logic.auth.authentication import authenticate_user

pytestmark = pytest.mark.db


def test_seeding_twice_creates_everything_once_and_the_users_can_log_in(db, db_sessions, monkeypatch, capsys):
    import scripts.seed_gpu_demo as demo

    monkeypatch.setattr(demo, "SessionLocal", db_sessions)
    demo.seed()
    demo.seed()

    assert db.scalar(select(func.count()).select_from(orm.Lab)) == 4
    assert db.scalar(select(func.count()).select_from(orm.User)) == 5   # with the demo professor
    contexts = {c.account_type for c in db.scalars(select(orm.SchedulingContext))}
    assert contexts == {"research", "course"} and db.scalar(select(func.count()).select_from(orm.SchedulingContext)) == 3
    admin = authenticate_user(db, "gpuadmin@iitk.ac.in", "nimdaupg")
    assert admin is not None and admin.role == "ADMIN"
    assert db.get(orm.SchedulingContext, admin.id) is None          # an admin never submits (FR-GPU-173)
    assert "cs455.student@iitk.ac.in" in capsys.readouterr().out
