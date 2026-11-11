"""
Property test of the allocation invariants (PRAYOG-105; NFR-GPU-001, 002, 003; A-65).

Random sequences of the operations that change allocation state, on a real database. Whatever
the order, after every step: no unit has two live allocations, no job has two, a placement holds
exactly the GPUs it asked for, unit markers agree with allocations, and a job's state agrees with
whether it holds anything.
"""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

import models as orm
from logic.common.errors import AppError
from logic.gpu.scheduler.cancel import cancel_job
from logic.gpu.scheduler.domain import JobState, ReleaseReason
from logic.gpu.scheduler.lifecycle import mark_running, release
from logic.gpu.scheduler.passes import Trigger, run_pass
from test_cases.gpu.scheduler.db_factories import Seed, broken_invariants
from test_cases.gpu.scheduler.factories import NOW

pytestmark = pytest.mark.db

Step = st.one_of(
    st.tuples(st.just("submit"), st.integers(1, 3)),
    st.tuples(st.just("pass"), st.just(0)),
    st.tuples(st.just("start"), st.integers(0, 9)),
    st.tuples(st.just("finish"), st.integers(0, 9)),
    st.tuples(st.just("node_failure"), st.integers(0, 9)),
    st.tuples(st.just("cancel"), st.integers(0, 9)),
)


def pick(session, states, index):
    jobs = session.scalars(select(orm.Job.id).where(orm.Job.state.in_(states)).order_by(orm.Job.id)).all()
    return jobs[index % len(jobs)] if jobs else None


def apply(sessions, lab, user, seed, step):
    action, value = step
    if action == "submit":
        seed.job(lab, gpus=value, user=user)
        seed.commit()
    elif action == "pass":
        run_pass(sessions, Trigger.TICK, now=NOW)
    else:
        with sessions() as session:
            states = {"start": [JobState.SCHEDULED], "finish": [JobState.RUNNING],
                      "node_failure": [JobState.RUNNING, JobState.SCHEDULED],
                      "cancel": [JobState.QUEUED, JobState.SCHEDULED, JobState.RUNNING]}[action]
            job = pick(session, states, value)
            if job is None:
                return
            if action == "start":
                mark_running(session, job, now=NOW)
            elif action == "finish":
                release(session, job, ReleaseReason.COMPLETED, now=NOW, exit_code=0)
            elif action == "node_failure":
                release(session, job, ReleaseReason.NODE_FAILURE, now=NOW)
            else:
                owner = session.get(orm.Job, job).user_id
                try:
                    cancel_job(session, job, owner, now=NOW)
                except AppError:
                    session.rollback()


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(steps=st.lists(Step, min_size=1, max_size=14))
def test_no_order_of_operations_breaks_the_allocation_invariants(db_engine, steps):
    tables = ", ".join(f'"{t.name}"' for t in orm.Base.metadata.sorted_tables)
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    sessions = sessionmaker(bind=db_engine, autoflush=False)

    with sessions() as session:
        seed = Seed(session)
        lab = seed.lab("NLP")
        user = seed.member(lab)
        nodes = [seed.node(lab, units=units) for units in (2, 3)]
        seed.commit()

        for step in steps:
            apply(sessions, lab, user, seed, step)
            session.rollback()                     # see what the other sessions committed
            assert broken_invariants(session) == {}, (step, steps)

        held = session.scalars(select(orm.GpuUnit.current_allocation_id).where(
            orm.GpuUnit.node_id.in_(nodes), orm.GpuUnit.current_allocation_id.is_not(None))).all()
        placed = session.scalars(select(orm.Job.gpus).where(orm.Job.state.in_([JobState.SCHEDULED, JobState.RUNNING]))).all()
        assert len(held) == sum(placed)            # every GPU in use is accounted for by a placed job
