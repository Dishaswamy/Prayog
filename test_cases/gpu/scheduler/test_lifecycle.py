"""Starting a placed job and releasing its GPUs, with GPU-time accounting (GPU requirements section 7.5)."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, update

import models as orm
from logic.common.errors import ConflictError, NotFoundError
from logic.gpu.scheduler.claim import claim
from logic.gpu.scheduler.domain import JobState, PriorityClass, ReleaseReason
from logic.gpu.scheduler.lifecycle import mark_running, release
from logic.gpu.scheduler.loading import load_usage
from logic.gpu.scheduler.passes import Trigger, run_pass
from logic.gpu.scheduler.scoring import NO_USAGE, score_job
from test_cases.gpu.scheduler.db_factories import broken_invariants
from test_cases.gpu.scheduler.factories import NOW, job

pytestmark = pytest.mark.db

START = NOW + timedelta(minutes=1)
END = START + timedelta(hours=2)


def placed(seed, db_sessions, node, lab, gpus=2, course=False):
    """A job claimed on the node, still SCHEDULED."""
    j = seed.job(lab, gpus=gpus, course=course)
    seed.commit()
    with db_sessions() as s:
        assert claim(s, j, node, PriorityClass.P0, score_job(job(), NOW), pass_id="p", trigger="test", now=NOW).claimed
    return j


def running(seed, db_sessions, node, lab, gpus=2, course=False):
    j = placed(seed, db_sessions, node, lab, gpus, course)
    with db_sessions() as s:
        mark_running(s, j, now=START)
    return j


def end(db_sessions, job_id, reason, now=END):
    with db_sessions() as s:
        return release(s, job_id, reason, now=now)


def fresh(db, model, key):
    db.expire_all()
    return db.get(model, key)


def usage_rows(db):
    db.expire_all()
    return db.scalars(select(orm.UsageEntry)).all()


# ── start ──────────────────────────────────────────────────────────────

def test_acknowledged_start_moves_the_job_to_running(db, db_sessions, seed, labs):  # FR-GPU-126, FR-GPU-100
    node = seed.node(labs["NLP"])
    j = running(seed, db_sessions, node, labs["NLP"])

    assert fresh(db, orm.Job, j).state == JobState.RUNNING
    allocation = db.scalars(select(orm.Allocation)).one()
    assert allocation.acknowledged_at == START and allocation.released_at is None


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.COMPLETED])
def test_only_a_scheduled_job_can_start(db_sessions, seed, labs, state):
    j = seed.job(labs["NLP"], state=state)
    seed.commit()
    with pytest.raises(ConflictError, match="not SCHEDULED"), db_sessions() as s:
        mark_running(s, j, now=START)


# ── terminal releases ──────────────────────────────────────────────────

@pytest.mark.parametrize("reason", [ReleaseReason.COMPLETED, ReleaseReason.FAILED,
                                    ReleaseReason.WALLTIME_EXCEEDED, ReleaseReason.CANCELLED])
def test_a_finished_run_frees_its_units_and_is_charged(db, db_sessions, seed, labs, reason):  # FR-GPU-057, 100, 143
    node = seed.node(labs["NLP"], units=4)
    j = running(seed, db_sessions, node, labs["NLP"], gpus=2)

    result = end(db_sessions, j, reason)

    assert result.job_state == JobState(reason.value) and result.charged
    assert result.gpu_seconds == 2 * 2 * 3600                       # 2 units x 2 h, start to end only
    assert fresh(db, orm.Job, j).state == reason.value
    allocation = db.scalars(select(orm.Allocation)).one()
    assert (allocation.released_at, allocation.release_reason) == (END, reason)
    assert db.scalars(select(orm.AllocationUnit.released_at)).all() == [END, END]
    assert db.scalars(select(orm.GpuUnit.current_allocation_id)).all() == [None] * 4
    [entry] = usage_rows(db)
    assert (entry.job_id, entry.consumer_lab_id, entry.provider_lab_id, entry.gpu_seconds, entry.ended_at,
            entry.charged) == (j, labs["NLP"], labs["NLP"], 14400, END, True)
    assert broken_invariants(db) == {}


def test_cancelling_a_scheduled_job_rolls_back_its_claim_without_charge(db, db_sessions, seed, labs):  # FR-GPU-067
    node = seed.node(labs["NLP"], units=2)
    j = placed(seed, db_sessions, node, labs["NLP"])

    result = end(db_sessions, j, ReleaseReason.CANCELLED)

    assert (result.job_state, result.gpu_seconds, result.charged) == (JobState.CANCELLED, 0.0, False)
    assert usage_rows(db) == []                                     # never started: nothing to charge
    assert db.scalars(select(orm.GpuUnit.current_allocation_id)).all() == [None, None]


@pytest.mark.parametrize("reason", [ReleaseReason.COMPLETED, ReleaseReason.FAILED, ReleaseReason.WALLTIME_EXCEEDED])
def test_a_run_that_never_started_cannot_complete_or_fail(db_sessions, seed, labs, reason):
    node = seed.node(labs["NLP"])
    j = placed(seed, db_sessions, node, labs["NLP"])
    with pytest.raises(ConflictError, match="cannot end it"):
        end(db_sessions, j, reason)


# ── requeues ───────────────────────────────────────────────────────────

def test_node_failure_requeues_with_the_original_submit_time_and_no_charge(db, db_sessions, seed, labs):
    # FR-GPU-135, 143, NFR-GPU-016; a guest run still counts as served for the host lab
    rob_02 = seed.node(labs["ROBOTICS"], units=4)
    j = running(seed, db_sessions, rob_02, labs["NLP"], gpus=2)
    submitted = fresh(db, orm.Job, j).effective_submit_at

    result = end(db_sessions, j, ReleaseReason.NODE_FAILURE)

    row = fresh(db, orm.Job, j)
    assert (row.state, row.state_reason, row.effective_submit_at) == (JobState.QUEUED, "NODE_FAILURE", submitted)
    assert result.charged is False and result.gpu_seconds == 14400
    usage, total = load_usage(db, END)
    assert usage[labs["ROBOTICS"]].served_gpu_hours == 4.0          # credited to the host
    assert usage.get(labs["NLP"], NO_USAGE).consumed_gpu_hours == 0.0   # not charged to the guest
    assert total == 0.0
    assert broken_invariants(db) == {}


def test_dispatch_timeout_requeues_a_job_that_never_started(db, db_sessions, seed, labs):  # FR-GPU-126
    node = seed.node(labs["NLP"])
    j = placed(seed, db_sessions, node, labs["NLP"])

    result = end(db_sessions, j, ReleaseReason.DISPATCH_TIMEOUT)

    assert (result.job_state, fresh(db, orm.Job, j).state_reason) == (JobState.QUEUED, "DISPATCH_TIMEOUT")
    assert usage_rows(db) == []


def test_a_started_run_cannot_time_out_on_dispatch(db_sessions, seed, labs):  # FR-GPU-126
    node = seed.node(labs["NLP"])
    j = running(seed, db_sessions, node, labs["NLP"])
    with pytest.raises(ConflictError):
        end(db_sessions, j, ReleaseReason.DISPATCH_TIMEOUT)


def test_course_runs_are_charged_to_the_sponsorship_group(db, db_sessions, seed, labs):  # FR-GPU-120, 180
    teach = seed.node(labs["CENTRAL"], units=2, teaching=True)
    j = running(seed, db_sessions, teach, labs["COURSE"], gpus=1, course=True)

    end(db_sessions, j, ReleaseReason.COMPLETED)

    [entry] = usage_rows(db)
    assert (entry.consumer_lab_id, entry.provider_lab_id) == (labs["COURSE"], labs["CENTRAL"])


# ── never twice, and the units go back to work ─────────────────────────

def test_an_allocation_is_never_released_twice(db, db_sessions, seed, labs):  # NFR-GPU-061
    node = seed.node(labs["NLP"])
    j = running(seed, db_sessions, node, labs["NLP"])
    end(db_sessions, j, ReleaseReason.COMPLETED)

    with pytest.raises(ConflictError):
        end(db_sessions, j, ReleaseReason.CANCELLED, now=END + timedelta(minutes=1))
    assert len(usage_rows(db)) == 1


def test_concurrent_releases_of_one_job_succeed_exactly_once(db, db_sessions, seed, labs):  # NFR-GPU-061
    node = seed.node(labs["NLP"], units=4)
    j = running(seed, db_sessions, node, labs["NLP"])
    reasons = [ReleaseReason.COMPLETED, ReleaseReason.CANCELLED, ReleaseReason.NODE_FAILURE, ReleaseReason.FAILED]
    barrier = threading.Barrier(len(reasons))
    outcomes = []

    def attempt(reason):
        with db_sessions() as s:
            barrier.wait(timeout=10)
            try:
                outcomes.append(release(s, j, reason, now=END).job_state)
            except ConflictError:
                outcomes.append("refused")

    threads = [threading.Thread(target=attempt, args=(r,)) for r in reasons]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(map(str, outcomes)).count("refused") == 3
    assert len(usage_rows(db)) == 1
    assert broken_invariants(db) == {}


def test_a_scheduled_job_without_an_allocation_is_refused_not_half_released(db, db_sessions, seed, labs):
    # Only reachable if the data were already inconsistent; nothing may be written then.
    j = seed.job(labs["NLP"], state=JobState.SCHEDULED)
    seed.commit()
    with pytest.raises(ConflictError, match="holds no live allocation"):
        end(db_sessions, j, ReleaseReason.CANCELLED)
    assert fresh(db, orm.Job, j).state == JobState.SCHEDULED and usage_rows(db) == []


def test_a_start_and_a_dispatch_timeout_racing_leave_one_consistent_outcome(db, db_sessions, seed, labs):
    # FR-GPU-126: a timeout must never requeue a job whose start was just confirmed, nor the reverse.
    for _ in range(20):
        node = seed.node(labs["NLP"], units=1)
        j = placed(seed, db_sessions, node, labs["NLP"], gpus=1)
        barrier = threading.Barrier(2)
        outcome = {}

        def start(j=j, barrier=barrier, outcome=outcome):
            with db_sessions() as s:
                barrier.wait(timeout=10)
                try:
                    mark_running(s, j, now=START)
                    outcome["start"] = "ok"
                except ConflictError:
                    outcome["start"] = "refused"

        def time_out(j=j, barrier=barrier, outcome=outcome):
            with db_sessions() as s:
                barrier.wait(timeout=10)
                try:
                    release(s, j, ReleaseReason.DISPATCH_TIMEOUT, now=START)
                    outcome["timeout"] = "ok"
                except ConflictError:
                    outcome["timeout"] = "refused"

        threads = [threading.Thread(target=start), threading.Thread(target=time_out)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        state = fresh(db, orm.Job, j).state
        assert sorted(outcome.values()) == ["ok", "refused"], outcome
        assert state == (JobState.RUNNING if outcome["start"] == "ok" else JobState.QUEUED)
        assert broken_invariants(db) == {}


def test_a_dispatch_timeout_waits_for_a_start_in_progress_and_then_refuses(db, db_sessions, seed, labs):
    # FR-GPU-126, deterministic: the agent's start holds the job row; the timeout must wait for it,
    # re-read the job, and refuse, rather than requeue a job that is now RUNNING.
    node = seed.node(labs["NLP"], units=1)
    j = placed(seed, db_sessions, node, labs["NLP"], gpus=1)
    outcome = {}

    def time_out():
        with db_sessions() as s:
            try:
                release(s, j, ReleaseReason.DISPATCH_TIMEOUT, now=START)
                outcome["timeout"] = "ok"
            except ConflictError:
                outcome["timeout"] = "refused"

    with db_sessions() as agent_start:
        agent_start.execute(select(orm.Job).where(orm.Job.id == j).with_for_update())
        racer = threading.Thread(target=time_out)
        racer.start()
        racer.join(timeout=0.5)
        assert racer.is_alive()                                          # waiting on the job row
        agent_start.execute(update(orm.Job).where(orm.Job.id == j).values(state=JobState.RUNNING))
        agent_start.execute(update(orm.Allocation).where(orm.Allocation.job_id == j).values(acknowledged_at=START))
        agent_start.commit()
    racer.join(timeout=30)

    assert outcome == {"timeout": "refused"}
    assert fresh(db, orm.Job, j).state == JobState.RUNNING
    assert broken_invariants(db) == {}


def test_unknown_job_is_not_found(db_sessions, labs):
    with pytest.raises(NotFoundError):
        end(db_sessions, 99999, ReleaseReason.COMPLETED)


def test_released_units_are_placed_by_the_next_pass(db, db_sessions, seed, labs):  # FR-GPU-121 (run end)
    node = seed.node(labs["NLP"], units=2)
    first = running(seed, db_sessions, node, labs["NLP"], gpus=2)
    waiting = seed.job(labs["NLP"], gpus=2)
    seed.commit()
    assert run_pass(db_sessions, Trigger.TICK, now=START).reasons[waiting] == "INSUFFICIENT_FREE_UNITS"

    end(db_sessions, first, ReleaseReason.COMPLETED)
    result = run_pass(db_sessions, Trigger.RUN_END, now=END)

    assert waiting in result.placed
    assert broken_invariants(db) == {}
