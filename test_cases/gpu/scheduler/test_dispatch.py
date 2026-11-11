"""Dispatch to node agents and cancellation (GPU requirements sections 7.5 and 7.6)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

import models as orm
from logic.common.errors import BadRequestError, ConflictError, NotFoundError
from logic.gpu.scheduler import dispatch
from logic.gpu.scheduler.cancel import cancel_job
from logic.gpu.scheduler.dispatch import Outcome
from logic.gpu.scheduler.domain import JobState, ReleaseReason, RunEndState
from test_cases.gpu.scheduler.db_factories import broken_invariants, place, start
from test_cases.gpu.scheduler.factories import NOW

pytestmark = pytest.mark.db

LATER = NOW + timedelta(hours=1)


def job_row(db, job_id):
    db.expire_all()
    return db.get(orm.Job, job_id)


def run_row(db, job_id):
    db.expire_all()
    return db.scalars(select(orm.Run).where(orm.Run.job_id == job_id)).one()


@pytest.fixture
def placed(db_sessions, seed, labs):
    """A 2-GPU job claimed on a 4-unit NLP node, not yet acknowledged."""
    node = seed.node(labs["NLP"], units=4)
    other = seed.node(labs["NLP"], units=4)
    owner = seed.member(labs["NLP"])
    j = seed.job(labs["NLP"], gpus=2, user=owner)
    seed.commit()
    allocation = place(db_sessions, j, node)
    return {"node": node, "other_node": other, "job": j, "allocation": allocation, "owner": owner}


# ── what the agent sees (FR-GPU-059, 060, 129) ─────────────────────────

def test_an_agent_sees_only_its_own_nodes_allocations(db, placed):  # FR-GPU-059, 129
    [order] = dispatch.allocations_for(db, placed["node"])
    assert (order.allocation_id, order.job_id, order.unit_indexes) == (placed["allocation"], placed["job"], (0, 1))
    assert (order.walltime_minutes, order.started_at, order.stop_reason) == (240, None, None)
    assert dispatch.allocations_for(db, placed["other_node"]) == []


def test_acknowledging_starts_the_run_once(db, placed):  # FR-GPU-061, 064; NFR-GPU-019
    assert dispatch.acknowledge(db, placed["node"], placed["allocation"], now=NOW) is True
    assert dispatch.acknowledge(db, placed["node"], placed["allocation"], now=LATER) is False   # repeat delivery

    assert job_row(db, placed["job"]).state == JobState.RUNNING
    run = run_row(db, placed["job"])
    assert (run.allocation_id, run.node_id, run.started_at, run.ended_at) == (
        placed["allocation"], placed["node"], NOW, None)


def test_another_nodes_allocation_is_not_found(db, placed):  # FR-GPU-129
    with pytest.raises(NotFoundError):
        dispatch.acknowledge(db, placed["other_node"], placed["allocation"], now=NOW)
    with pytest.raises(NotFoundError):
        dispatch.report(db, placed["other_node"], placed["allocation"], 1, Outcome.EXITED, 0, now=NOW)


def test_an_allocation_released_before_the_start_must_not_start(db, db_sessions, placed):  # architecture 7.5
    cancel_job(db, placed["job"], placed["owner"], now=NOW)                  # the owner cancels first

    with pytest.raises(ConflictError, match="do not start"):
        dispatch.acknowledge(db, placed["node"], placed["allocation"], now=NOW)
    assert job_row(db, placed["job"]).state == JobState.CANCELLED


# ── reports (FR-GPU-069, 128) ──────────────────────────────────────────

@pytest.mark.parametrize("outcome, code, job_state", [
    (Outcome.EXITED, 0, JobState.COMPLETED),
    (Outcome.EXITED, 3, JobState.FAILED),
    (Outcome.WALLTIME, None, JobState.WALLTIME_EXCEEDED),             # the agent's own timer (FR-GPU-134)
    (Outcome.STOPPED, None, JobState.FAILED),                          # stopped without being asked
])
def test_a_report_ends_the_run_and_releases_its_units(db, placed, outcome, code, job_state):  # FR-GPU-069
    dispatch.acknowledge(db, placed["node"], placed["allocation"], now=NOW)

    result = dispatch.report(db, placed["node"], placed["allocation"], 1, outcome, code, now=LATER)

    assert result.job_state == job_state and job_row(db, placed["job"]).state == job_state
    run = run_row(db, placed["job"])
    assert (run.ended_at, run.end_state, run.exit_code) == (LATER, job_state.value, code)
    assert dispatch.allocations_for(db, placed["node"]) == []
    assert broken_invariants(db) == {}


def test_each_report_is_applied_at_most_once(db, placed):  # FR-GPU-128
    dispatch.acknowledge(db, placed["node"], placed["allocation"], now=NOW)
    assert dispatch.report(db, placed["node"], placed["allocation"], 5, Outcome.EXITED, 0, now=LATER) is not None
    assert dispatch.report(db, placed["node"], placed["allocation"], 5, Outcome.EXITED, 0, now=LATER) is None
    assert dispatch.report(db, placed["node"], placed["allocation"], 4, Outcome.EXITED, 0, now=LATER) is None
    assert len(db.scalars(select(orm.UsageEntry)).all()) == 1


def test_bad_reports_are_refused(db, placed):
    with pytest.raises(ConflictError, match="never started"):
        dispatch.report(db, placed["node"], placed["allocation"], 1, Outcome.EXITED, 0, now=LATER)
    dispatch.acknowledge(db, placed["node"], placed["allocation"], now=NOW)
    with pytest.raises(BadRequestError, match="exit code"):
        dispatch.report(db, placed["node"], placed["allocation"], 2, Outcome.EXITED, None, now=LATER)


# ── cancel (FR-GPU-065 to 069) ─────────────────────────────────────────

def test_cancelling_a_queued_job_releases_nothing(db, seed, labs):  # FR-GPU-066
    owner = seed.member(labs["NLP"])
    j = seed.job(labs["NLP"], user=owner)
    seed.commit()

    result = cancel_job(db, j, owner, now=NOW)

    assert (result.state, result.stop_pending) == (JobState.CANCELLED, False)
    assert (job_row(db, j).state, job_row(db, j).cancel_reason) == (JobState.CANCELLED, "owner")


def test_cancelling_a_scheduled_job_rolls_back_its_claim(db, placed):  # FR-GPU-067
    result = cancel_job(db, placed["job"], placed["owner"], now=NOW)

    assert (result.state, result.stop_pending) == (JobState.CANCELLED, False)
    assert dispatch.allocations_for(db, placed["node"]) == []
    assert broken_invariants(db) == {}


def test_cancelling_a_running_job_waits_for_the_agent(db, placed):  # FR-GPU-068, 069; A-11
    dispatch.acknowledge(db, placed["node"], placed["allocation"], now=NOW)

    result = cancel_job(db, placed["job"], placed["owner"], now=LATER)

    assert (result.state, result.stop_pending) == (JobState.RUNNING, True)
    [order] = dispatch.allocations_for(db, placed["node"])
    assert order.stop_reason == ReleaseReason.CANCELLED                      # the units are still held
    dispatch.report(db, placed["node"], placed["allocation"], 1, Outcome.STOPPED, None, now=LATER)
    assert job_row(db, placed["job"]).state == JobState.CANCELLED
    assert run_row(db, placed["job"]).end_state == RunEndState.CANCELLED
    entry = db.scalars(select(orm.UsageEntry)).one()
    assert (entry.charged, entry.gpu_seconds) == (True, 2 * 3600)          # cancelled runs are charged (FR-GPU-143)


def test_the_first_stop_reason_wins(db, placed):  # FR-GPU-068
    dispatch.acknowledge(db, placed["node"], placed["allocation"], now=NOW)
    cancel_job(db, placed["job"], placed["owner"], now=LATER)
    cancel_job(db, placed["job"], placed["owner"], now=LATER)                # a second cancel changes nothing
    [order] = dispatch.allocations_for(db, placed["node"])
    assert order.stop_reason == ReleaseReason.CANCELLED


def test_only_the_owner_may_cancel(db, seed, labs, placed, caplog):  # FR-GPU-065
    stranger = seed.member(labs["NLP"])
    seed.commit()
    with pytest.raises(NotFoundError):
        cancel_job(db, placed["job"], stranger, now=NOW)
    assert "refused" in caplog.text                                         # the attempt is logged
    assert job_row(db, placed["job"]).state == JobState.SCHEDULED


@pytest.mark.parametrize("state", [JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED])
def test_a_finished_job_cannot_be_cancelled(db, seed, labs, state):
    owner = seed.member(labs["NLP"])
    j = seed.job(labs["NLP"], user=owner, state=state)
    seed.commit()
    with pytest.raises(ConflictError, match="already"):
        cancel_job(db, j, owner, now=NOW)


def test_cancelling_an_unknown_job_is_not_found(db, labs):
    with pytest.raises(NotFoundError):
        cancel_job(db, 9999, 1, now=NOW)


def test_start_helper_matches_an_acknowledgement(db, db_sessions, placed):
    start(db_sessions, placed["job"], now=NOW)
    assert dispatch.acknowledge(db, placed["node"], placed["allocation"], now=NOW) is False
