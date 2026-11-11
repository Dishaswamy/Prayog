"""The scheduling pass against a real PostgreSQL (GPU requirements section 7.4)."""

import json
import logging
import threading

import pytest
from sqlalchemy import select

import models as orm
from logic.gpu.scheduler.claim import claim
from logic.gpu.scheduler.config import DEFAULT_CONFIG
from logic.gpu.scheduler.domain import JobState, NodeState, PriorityClass, QueueReason
from logic.gpu.scheduler.loading import load_usage
from logic.gpu.scheduler.passes import Trigger, _record_reasons, run_pass
from logic.gpu.scheduler.run import run_ticks
from logic.gpu.scheduler.scoring import score_job
from test_cases.gpu.scheduler.db_factories import broken_invariants
from test_cases.gpu.scheduler.factories import NOW, job

pytestmark = pytest.mark.db

P0, P1, P2, P3 = PriorityClass
T_MAX = DEFAULT_CONFIG.t_max_minutes


def run(db_sessions, trigger=Trigger.TICK):
    return run_pass(db_sessions, trigger, now=NOW)


def placement(db, job_id):
    """(node id, unit indexes) of the job's live allocation, or None."""
    db.expire_all()
    rows = db.execute(
        select(orm.Allocation.node_id, orm.GpuUnit.unit_index)
        .join(orm.AllocationUnit, orm.AllocationUnit.allocation_id == orm.Allocation.id)
        .join(orm.GpuUnit, orm.GpuUnit.id == orm.AllocationUnit.unit_id)
        .where(orm.Allocation.job_id == job_id, orm.Allocation.released_at.is_(None))
        .order_by(orm.GpuUnit.unit_index)
    ).all()
    return (rows[0][0], [r[1] for r in rows]) if rows else None


def job_row(db, job_id):
    db.expire_all()
    return db.get(orm.Job, job_id)


def occupy(seed, db_sessions, node, lab, gpus):
    """Hold units on a node with an earlier, already scheduled job."""
    j = seed.job(lab, gpus=gpus)
    seed.commit()
    with db_sessions() as session:
        assert claim(session, j, node, P0, score_job(job(), NOW), pass_id="earlier", trigger="test", now=NOW).claimed
    return j


# ── placement ──────────────────────────────────────────────────────────

def test_a_queued_job_is_scheduled_and_the_decision_names_the_pass(db, db_sessions, seed, labs):  # FR-GPU-030, 058
    node = seed.node(labs["NLP"], units=4)
    j = seed.job(labs["NLP"], gpus=2)
    seed.commit()

    result = run(db_sessions, Trigger.SUBMIT)

    assert job_row(db, j).state == JobState.SCHEDULED
    assert placement(db, j) == (node, [0, 1])                                   # lowest units (FR-GPU-123)
    assert result.reasons == {} and set(result.placed) == {j}
    decision = db.scalars(select(orm.SchedulingDecision)).one()
    assert (decision.pass_id, decision.trigger, decision.priority_class) == (result.pass_id, "submit", P0)


def test_best_fit_picks_the_node_with_the_fewest_usable_units(db, db_sessions, seed, labs):  # A-63, FR-GPU-122
    big = seed.node(labs["NLP"], units=8)
    small = seed.node(labs["NLP"], units=2)
    j = seed.job(labs["NLP"], gpus=2)
    seed.commit()

    run(db_sessions)

    assert placement(db, j)[0] == small != big


def test_equal_fit_breaks_by_lower_node_id(db, db_sessions, seed, labs):  # FR-GPU-122
    first = seed.node(labs["NLP"], units=4)
    seed.node(labs["NLP"], units=4)
    j = seed.job(labs["NLP"], gpus=2)
    seed.commit()

    run(db_sessions)

    assert placement(db, j)[0] == first


def test_best_fit_sees_units_taken_earlier_in_the_pass(db, db_sessions, seed, labs):  # FR-GPU-122
    four = seed.node(labs["NLP"], units=4)
    three = seed.node(labs["NLP"], units=3)
    a = seed.job(labs["NLP"], gpus=3, waited_min=30)              # ranks first, best fit: the 3-unit node
    b = seed.job(labs["NLP"], gpus=2)                              # 3-unit node now full: goes to the 4-unit node
    seed.commit()

    run(db_sessions)

    assert placement(db, a) == (three, [0, 1, 2])
    assert placement(db, b) == (four, [0, 1])


def test_a_job_is_never_split_across_nodes(db, db_sessions, seed, labs):  # A-80, FR-GPU-183
    nodes = [seed.node(labs["NLP"], units=4) for _ in range(2)]
    for node in nodes:
        occupy(seed, db_sessions, node, labs["NLP"], gpus=2)       # 2 free on each, 4 free in all
    j = seed.job(labs["NLP"], gpus=4)
    seed.commit()

    result = run(db_sessions)

    assert placement(db, j) is None
    assert result.reasons[j] == QueueReason.INSUFFICIENT_FREE_UNITS
    assert broken_invariants(db) == {}


# ── classes and ranking ────────────────────────────────────────────────

@pytest.mark.parametrize("guest_waited_min", [120, T_MAX + 60])
def test_class_dominates_score_and_escalation(db, db_sessions, seed, labs, guest_waited_min):
    # NFR-GPU-010, FR-GPU-043, A-60 (escalation does not cross classes)
    rob_02 = seed.node(labs["ROBOTICS"], units=2)
    guest = seed.job(labs["NLP"], gpus=2, waited_min=guest_waited_min)   # far higher score, or escalated
    owner = seed.job(labs["ROBOTICS"], gpus=2)                            # P0, just submitted
    seed.commit()

    result = run(db_sessions)

    assert placement(db, owner)[0] == rob_02
    assert placement(db, guest) is None
    assert result.reasons[guest] == QueueReason.OUTRANKED


def test_within_a_class_the_older_job_goes_first(db, db_sessions, seed, labs):  # FR-GPU-044
    seed.node(labs["NLP"], units=2)
    newer = seed.job(labs["NLP"], gpus=2, waited_min=10)
    older = seed.job(labs["NLP"], gpus=2, waited_min=50)
    seed.commit()

    result = run(db_sessions)

    assert set(result.placed) == {older}
    assert result.reasons == {newer: QueueReason.OUTRANKED}


def test_equal_scores_and_submit_times_go_by_lower_job_id(db, db_sessions, seed, labs):  # A-64, FR-GPU-119
    seed.node(labs["NLP"], units=2)
    first, second = (seed.job(labs["NLP"], gpus=2, waited_min=30) for _ in range(2))
    seed.commit()

    assert set(run(db_sessions).placed) == {first}
    assert job_row(db, second).state_reason == QueueReason.OUTRANKED


def test_fair_share_favours_the_lab_that_used_less(db, db_sessions, seed, labs):  # FR-GPU-046/047
    seed.node(labs["VISION"], units=2)                             # shared, Vision has nothing queued: both P1
    seed.usage(consumer=labs["NLP"], provider=labs["NLP"], gpu_hours=100)
    heavy = seed.job(labs["NLP"], gpus=2, waited_min=30)
    light = seed.job(labs["ROBOTICS"], gpus=2, waited_min=30)
    seed.commit()

    result = run(db_sessions)

    assert set(result.placed) == {light}
    assert result.reasons == {heavy: QueueReason.OUTRANKED}


def test_usage_is_decayed_and_contribution_is_windowed(db, seed, labs):  # FR-GPU-047/048
    nlp, rob = labs["NLP"], labs["ROBOTICS"]
    seed.usage(consumer=nlp, provider=rob, gpu_hours=10)                       # NLP borrowed from Robotics
    seed.usage(consumer=nlp, provider=nlp, gpu_hours=6)                        # own node: neither served nor consumed
    seed.usage(consumer=rob, provider=nlp, gpu_hours=4, ended_h_ago=31 * 24)   # outside the 30-day window
    seed.commit()

    usage, total = load_usage(db, NOW)

    assert usage[nlp].consumed_gpu_hours == 10 and usage[nlp].served_gpu_hours == 0
    assert usage[rob].served_gpu_hours == 10 and usage[rob].consumed_gpu_hours == 0
    assert usage[nlp].decayed_gpu_hours == pytest.approx(16 * 2 ** (-1 / (7 * 24)))
    assert usage[rob].decayed_gpu_hours == pytest.approx(4 * 2 ** (-31 / 7))
    assert total == pytest.approx(usage[nlp].decayed_gpu_hours + usage[rob].decayed_gpu_hours)


# ── queue reasons (FR-GPU-144) ─────────────────────────────────────────

def test_a_job_that_cannot_fit_now_stays_queued_with_a_reason(db, db_sessions, seed, labs):  # A-03, FR-GPU-035
    node = seed.node(labs["NLP"], units=4)
    occupy(seed, db_sessions, node, labs["NLP"], gpus=3)
    j = seed.job(labs["NLP"], gpus=2, walltime_h=4)
    seed.commit()

    run(db_sessions)

    row = job_row(db, j)
    assert (row.state, row.state_reason) == (JobState.QUEUED, QueueReason.INSUFFICIENT_FREE_UNITS)


def test_maintenance_reason_when_only_a_window_is_in_the_way(db, db_sessions, seed, labs):  # A-13, FR-GPU-034
    node = seed.node(labs["NLP"], units=4)
    seed.window(seed.unit_ids(node)[0], start_in_h=6)
    j = seed.job(labs["NLP"], gpus=4, walltime_h=8)               # would still be running when the window opens
    seed.commit()

    run(db_sessions)

    assert job_row(db, j).state_reason == QueueReason.MAINTENANCE_WINDOW


def test_research_job_never_runs_on_the_teaching_pool(db, db_sessions, seed, labs):  # A-61, FR-GPU-032
    seed.node(labs["CENTRAL"], units=8, teaching=True)
    j = seed.job(labs["NLP"], gpus=2)
    seed.commit()

    run(db_sessions)

    row = job_row(db, j)
    assert (row.state, row.state_reason) == (JobState.QUEUED, QueueReason.NO_ELIGIBLE_NODE)


def test_course_job_runs_only_on_the_teaching_pool(db, db_sessions, seed, labs):  # A-19, FR-GPU-032, P3
    seed.node(labs["VISION"], units=4)                             # shared research node, idle
    teaching = seed.node(labs["CENTRAL"], units=4, teaching=True)
    j = seed.job(labs["COURSE"], gpus=1, course=True)
    seed.commit()

    run(db_sessions)

    assert placement(db, j)[0] == teaching
    assert db.scalars(select(orm.SchedulingDecision.priority_class)).one() == P3


@pytest.mark.parametrize("state", [NodeState.DRAINING, NodeState.DOWN])
def test_nodes_that_are_not_available_receive_nothing(db, db_sessions, seed, labs, state):  # FR-GPU-118, NFR-GPU-059
    seed.node(labs["NLP"], units=4, state=state)
    j = seed.job(labs["NLP"], gpus=1)
    seed.commit()

    run(db_sessions)

    assert job_row(db, j).state_reason == QueueReason.NO_ELIGIBLE_NODE


def test_scheduled_and_running_jobs_are_not_touched(db, db_sessions, seed, labs):  # FR-GPU-124
    node = seed.node(labs["NLP"], units=4)
    scheduled = occupy(seed, db_sessions, node, labs["NLP"], gpus=2)
    running = seed.job(labs["NLP"], gpus=1, state=JobState.RUNNING)
    seed.commit()
    before = placement(db, scheduled)

    result = run(db_sessions)

    assert scheduled not in result.placed and running not in result.placed and result.queued == 0
    assert placement(db, scheduled) == before
    assert job_row(db, running).state == JobState.RUNNING and job_row(db, running).state_reason is None


def test_a_job_placed_after_waiting_loses_its_old_reason(db, db_sessions, seed, labs):  # FR-GPU-021
    node = seed.node(labs["NLP"], units=2, state=NodeState.DOWN)
    j = seed.job(labs["NLP"], gpus=2)
    seed.commit()
    run(db_sessions)
    assert job_row(db, j).state_reason == QueueReason.NO_ELIGIBLE_NODE

    db.get(orm.Node, node).state = NodeState.AVAILABLE
    db.commit()
    run(db_sessions, Trigger.NODE_STATE)

    row = job_row(db, j)
    assert (row.state, row.state_reason) == (JobState.SCHEDULED, None)


def test_a_reason_never_lands_on_a_job_another_pass_placed_meanwhile(db, db_sessions, seed, labs):  # FR-GPU-124
    # Pass A read the job as QUEUED; pass B placed it before A wrote its reasons.
    node = seed.node(labs["NLP"], units=2)
    placed_by_b = occupy(seed, db_sessions, node, labs["NLP"], gpus=2)

    with db_sessions() as session:
        _record_reasons(session, {placed_by_b: QueueReason.INSUFFICIENT_FREE_UNITS})
        session.commit()

    row = job_row(db, placed_by_b)
    assert (row.state, row.state_reason) == (JobState.SCHEDULED, None)


# ── concurrency, logging, the tick ─────────────────────────────────────

def test_concurrent_passes_place_each_job_once(db, db_sessions, seed, labs):  # FR-GPU-121 (concurrent passes), I1, I2
    for _ in range(3):
        seed.node(labs["NLP"], units=3)
    jobs = [seed.job(labs["NLP"], gpus=1, waited_min=i) for i in range(12)]
    seed.commit()

    barrier = threading.Barrier(4)
    errors = []

    def one_pass():
        try:
            barrier.wait(timeout=10)
            run(db_sessions)
        except Exception as exc:  # noqa: BLE001  surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=one_pass) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert broken_invariants(db) == {}
    run(db_sessions)                                               # whatever the race left, the next pass finishes
    assert sum(job_row(db, j).state == JobState.SCHEDULED for j in jobs) == 9
    assert broken_invariants(db) == {}


def test_each_pass_writes_one_structured_log_record(db_sessions, seed, labs, caplog):  # A-74
    seed.node(labs["NLP"], units=2)
    seed.job(labs["NLP"], gpus=2, waited_min=5)
    seed.job(labs["NLP"], gpus=2)
    seed.commit()

    with caplog.at_level(logging.INFO, logger="prayog.scheduler"):
        result = run(db_sessions, Trigger.SUBMIT)

    [line] = [r.getMessage() for r in caplog.records if r.name == "prayog.scheduler"]
    record = json.loads(line.split(" ", 2)[2])         # "scheduling pass {...}"
    assert record["pass_id"] == result.pass_id and record["trigger"] == "submit"
    assert record["queued"] == 2 and record["placed"] == 1
    assert record["pairs_per_class"]["P0"] == 2
    assert record["reasons"] == {"OUTRANKED": 1}
    assert record["duration_ms"] >= 0


def test_each_pass_is_recorded_for_metrics(db, db_sessions, seed, labs):  # A-74, NFR-GPU-051
    node = seed.node(labs["NLP"], units=4)
    j = seed.job(labs["NLP"], gpus=4)
    seed.commit()

    with db_sessions() as other_pass:                    # holds one unit, so the claim fails once
        other_pass.execute(select(orm.GpuUnit.id).where(orm.GpuUnit.node_id == node).limit(1).with_for_update())
        result = run(db_sessions, Trigger.SUBMIT)
        other_pass.rollback()

    row = db.get(orm.SchedulingPass, result.pass_id)
    assert (row.trigger, row.started_at, row.queued, row.placed, row.claim_failures) == ("submit", NOW, 1, 0, 1)
    assert row.duration_ms >= 0
    assert result.reasons == {j: QueueReason.LOST_CLAIM_RACE}


def test_the_tick_runs_a_pass_each_interval(db, db_sessions, seed, labs):  # FR-GPU-121, NFR-GPU-054
    seed.node(labs["NLP"], units=4)
    j = seed.job(labs["NLP"], gpus=1, waited_min=5)
    seed.commit()
    sleeps = []

    run_ticks(db_sessions, ticks=2, sleep=sleeps.append)

    assert sleeps == [DEFAULT_CONFIG.scheduler_tick_seconds] * 2
    assert job_row(db, j).state == JobState.SCHEDULED
    decision = db.scalars(select(orm.SchedulingDecision)).one()
    assert decision.trigger == "tick"


def test_a_failing_tick_pass_does_not_stop_the_tick(caplog):  # FR-GPU-121
    def broken_sessions():
        raise RuntimeError("database down")

    with caplog.at_level(logging.ERROR, logger="prayog.scheduler"):
        run_ticks(broken_sessions, ticks=2, sleep=lambda _: None)

    assert [r.getMessage() for r in caplog.records].count("tick pass failed") == 2
