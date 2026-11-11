"""The reaper and maintenance windows (GPU requirements sections 7.6 to 7.8)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

import models as orm
from logic.common.errors import BadRequestError, NotFoundError
from logic.gpu.scheduler import dispatch, maintenance, nodes
from logic.gpu.scheduler.config import DEFAULT_CONFIG
from logic.gpu.scheduler.dispatch import Outcome
from logic.gpu.scheduler.domain import AlertKind, JobState, NodeState, ReleaseReason
from logic.gpu.scheduler.passes import Trigger, run_pass
from logic.gpu.scheduler.reaper import reap
from test_cases.gpu.scheduler.db_factories import broken_invariants, place, start
from test_cases.gpu.scheduler.factories import NOW

pytestmark = pytest.mark.db

CFG = DEFAULT_CONFIG
S = timedelta(seconds=1)


def fresh(db, model, key):
    db.expire_all()
    return db.get(model, key)


def live_node(db, labs, units=4, name="nlp-01", owner="NLP"):
    """A registered, enrolled node that heartbeated at NOW and is AVAILABLE."""
    reg = nodes.register_node(db, name, labs[owner], "shared", False, units, now=NOW)
    nodes.enrol(db, reg.enrolment_token, now=NOW)
    nodes.heartbeat(db, reg.node.id, units, [], now=NOW)
    return reg.node.id


def stop_reason(db, job_id):
    db.expire_all()
    return db.scalar(select(orm.Allocation.stop_reason).where(
        orm.Allocation.job_id == job_id, orm.Allocation.released_at.is_(None)))


# ── node loss (FR-GPU-077, 079, 135; NFR-GPU-015, 016) ─────────────────

def test_a_silent_node_goes_down_and_its_jobs_are_requeued(db, db_sessions, seed, labs):  # A-17
    node = live_node(db, labs)
    running = seed.job(labs["NLP"], gpus=2)
    scheduled = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    place(db_sessions, running, node)
    place(db_sessions, scheduled, node)
    start(db_sessions, running, now=NOW)

    result = reap(db, NOW + (CFG.heartbeat_timeout_seconds + 1) * S, CFG)

    assert result.nodes_down == [node]
    assert fresh(db, orm.Node, node).state == NodeState.DOWN
    assert [a.kind for a in nodes.open_alerts(db)] == [AlertKind.NODE_DOWN]
    for j in (running, scheduled):
        row = fresh(db, orm.Job, j)
        assert (row.state, row.state_reason) == (JobState.QUEUED, "NODE_FAILURE")
    assert db.scalars(select(orm.UsageEntry.charged)).all() == [False]       # interrupted: not charged
    assert broken_invariants(db) == {}


def test_a_recent_heartbeat_keeps_the_node(db, labs):  # FR-GPU-077
    node = live_node(db, labs)
    assert reap(db, NOW + (CFG.heartbeat_timeout_seconds - 1) * S, CFG).nodes_down == []
    assert fresh(db, orm.Node, node).state == NodeState.AVAILABLE


def test_a_down_node_receives_nothing_and_comes_back_after_a_clean_heartbeat(db, db_sessions, seed, labs):
    # NFR-GPU-059 / FR-GPU-136: no placements while DOWN, back to AVAILABLE on rejoin
    node = live_node(db, labs)
    later = NOW + (CFG.heartbeat_timeout_seconds + 1) * S
    reap(db, later, CFG)
    j = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    assert run_pass(db_sessions, Trigger.TICK, now=later).reasons[j] == "NO_ELIGIBLE_NODE"

    nodes.heartbeat(db, node, 4, [], now=later)
    assert j in run_pass(db_sessions, Trigger.NODE_STATE, now=later).placed
    assert nodes.open_alerts(db) == []                                       # NODE_DOWN cleared


# ── dispatch timeouts (FR-GPU-126, 127) ────────────────────────────────

def test_an_unacknowledged_dispatch_is_requeued_then_failed(db, db_sessions, seed, labs):  # A-23
    node = live_node(db, labs)
    j = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    t = NOW
    for attempt in range(1, CFG.max_dispatch_attempts + 1):
        place(db_sessions, j, node, now=t)
        nodes.heartbeat(db, node, 4, [], now=t + (CFG.dispatch_ack_timeout_seconds + 1) * S)
        t = t + (CFG.dispatch_ack_timeout_seconds + 1) * S
        reap(db, t, CFG)
        row = fresh(db, orm.Job, j)
        assert row.dispatch_attempts == attempt
        if attempt < CFG.max_dispatch_attempts:
            assert (row.state, row.state_reason) == (JobState.QUEUED, "DISPATCH_TIMEOUT")
    assert (row.state, row.state_reason) == (JobState.FAILED, "DISPATCH_FAILED")
    assert db.scalars(select(orm.UsageEntry)).all() == []                    # never started: nothing charged


def test_an_acknowledged_dispatch_does_not_time_out(db, db_sessions, seed, labs):
    node = live_node(db, labs)
    j = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    place(db_sessions, j, node)
    start(db_sessions, j, now=NOW)
    nodes.heartbeat(db, node, 4, [j], now=NOW + 100 * S)
    assert reap(db, NOW + 100 * S, CFG).released == {}


# ── walltime backstop (FR-GPU-073, 074) ────────────────────────────────

def test_the_server_asks_for_a_stop_past_walltime_and_the_report_ends_it(db, db_sessions, seed, labs):  # A-12
    node = live_node(db, labs)
    j = seed.job(labs["NLP"], gpus=1, walltime_h=1)
    seed.commit()
    allocation = place(db_sessions, j, node)
    start(db_sessions, j, now=NOW)
    end = NOW + timedelta(hours=1)
    nodes.heartbeat(db, node, 4, [allocation], now=end + 10 * S)

    assert reap(db, end + (CFG.walltime_tolerance_seconds - 1) * S, CFG).stops_requested == {}
    assert reap(db, end + (CFG.walltime_tolerance_seconds + 1) * S, CFG).stops_requested == {j: "WALLTIME_EXCEEDED"}
    dispatch.report(db, node, allocation, 1, Outcome.STOPPED, None, now=end + 20 * S)
    assert fresh(db, orm.Job, j).state == JobState.WALLTIME_EXCEEDED


# ── hard drain and decommission (FR-GPU-080, 113 to 117) ───────────────

def test_a_hard_drain_stops_running_jobs_after_drain_grace(db, db_sessions, seed, labs):  # FR-GPU-080
    node = live_node(db, labs)
    j = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    allocation = place(db_sessions, j, node)
    start(db_sessions, j, now=NOW)
    nodes.drain(db, node, "hard", "PSU failing", now=NOW)
    grace = timedelta(minutes=CFG.drain_grace_minutes)
    nodes.heartbeat(db, node, 4, [allocation], now=NOW + grace)

    assert reap(db, NOW + grace - S, CFG).stops_requested == {}
    assert reap(db, NOW + grace, CFG).stops_requested == {j: "EMERGENCY_DRAIN"}
    dispatch.report(db, node, allocation, 1, Outcome.STOPPED, None, now=NOW + grace + 5 * S)
    row = fresh(db, orm.Job, j)
    assert (row.state, row.state_reason) == (JobState.QUEUED, "EMERGENCY_DRAIN")     # requeued, original age


def test_a_soft_drain_lets_running_jobs_finish(db, db_sessions, seed, labs):  # FR-GPU-168
    node = live_node(db, labs)
    j = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    allocation = place(db_sessions, j, node)
    start(db_sessions, j, now=NOW)
    nodes.drain(db, node, "soft", "planned", now=NOW)
    nodes.heartbeat(db, node, 4, [allocation], now=NOW + timedelta(hours=1))
    assert reap(db, NOW + timedelta(hours=1), CFG).stops_requested == {}


def test_a_decommission_completes_when_the_node_is_empty_and_revokes_it(db, db_sessions, seed, labs):
    # FR-GPU-113, 115, 116, 117; A-26
    node = live_node(db, labs, units=8)
    small = live_node(db, labs, units=2, name="nlp-02")
    running = seed.job(labs["NLP"], gpus=4)
    seed.commit()
    allocation = place(db_sessions, running, node)
    start(db_sessions, running, now=NOW)
    waiting = seed.job(labs["NLP"], gpus=8)                          # only the retiring node could ever run it
    seed.commit()
    nodes.decommission(db, node, "soft", "end of life", now=NOW)
    for n, running_ids in ((node, [allocation]), (small, [])):
        nodes.heartbeat(db, n, 8 if n == node else 2, running_ids, now=NOW + S)

    assert reap(db, NOW + S, CFG).decommissioned == []               # still running: stays DECOMMISSIONING
    dispatch.report(db, node, allocation, 1, Outcome.EXITED, 0, now=NOW + 2 * S)
    result = reap(db, NOW + 3 * S, CFG)

    assert result.decommissioned == [node] and result.cancelled_no_capable_node == [waiting]
    retired = fresh(db, orm.Node, node)
    assert (retired.state, retired.credential_revoked_at) == (NodeState.DECOMMISSIONED, NOW + 3 * S)
    assert nodes.authenticate(db, None) is None
    assert fresh(db, orm.Job, running).state == JobState.COMPLETED               # history kept
    assert db.scalars(select(orm.GpuUnit).where(orm.GpuUnit.node_id == node)).all()   # records kept (FR-GPU-116)


# ── maintenance (FR-GPU-081 to 085, 138) ───────────────────────────────

def test_preview_shows_clear_by_and_the_collision_set(db, db_sessions, seed, labs):  # FR-GPU-083, 084
    node = live_node(db, labs)
    owner = seed.member(labs["NLP"])
    j = seed.job(labs["NLP"], gpus=2, walltime_h=4, user=owner)
    seed.commit()
    place(db_sessions, j, node)
    start(db_sessions, j, now=NOW)
    units = seed.unit_ids(node)
    bound = timedelta(seconds=CFG.walltime_completion_bound_seconds)

    p = maintenance.preview(db, units, NOW + timedelta(hours=1), now=NOW)

    assert p.clear_by == NOW + timedelta(hours=4) + bound
    [c] = p.collisions
    assert (c.job_id, c.lab_id, c.gpu_hours_at_risk) == (j, labs["NLP"], 2.0)    # 2 GPUs x 1 h of work
    assert maintenance.preview(db, units, NOW + timedelta(hours=5), now=NOW).collisions == ()


def test_a_colliding_window_needs_confirmation(db, db_sessions, seed, labs):  # FR-GPU-084
    node = live_node(db, labs)
    j = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    place(db_sessions, j, node)
    start(db_sessions, j, now=NOW)
    units = seed.unit_ids(node)
    starts, ends = NOW + timedelta(hours=1), NOW + timedelta(hours=3)

    with pytest.raises(maintenance.CollisionNotConfirmed) as refused:
        maintenance.book(db, units, starts, ends, "firmware", now=NOW)
    assert refused.value.code == "MAINTENANCE_COLLISION" and len(refused.value.preview.collisions) == 1
    assert maintenance.bookings(db, now=NOW) == []

    booking_id, _ = maintenance.book(db, units, starts, ends, "firmware", confirm=True, now=NOW)
    [b] = maintenance.bookings(db, now=NOW)
    assert (b.booking_id, b.unit_ids, b.reason) == (booking_id, tuple(sorted(units)), "firmware")


def test_runs_still_active_when_a_window_opens_are_stopped_and_requeued(db, db_sessions, seed, labs):  # FR-GPU-085
    node = live_node(db, labs)
    j = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    allocation = place(db_sessions, j, node)
    start(db_sessions, j, now=NOW)
    opens = NOW + timedelta(hours=1)
    maintenance.book(db, seed.unit_ids(node)[:1], opens, opens + timedelta(hours=1), "fan", confirm=True, now=NOW)
    nodes.heartbeat(db, node, 4, [allocation], now=opens)

    assert reap(db, opens - S, CFG).stops_requested == {}
    assert reap(db, opens, CFG).stops_requested == {j: "MAINTENANCE"}
    dispatch.report(db, node, allocation, 1, Outcome.STOPPED, None, now=opens + 5 * S)
    row = fresh(db, orm.Job, j)
    assert (row.state, row.state_reason) == (JobState.QUEUED, ReleaseReason.MAINTENANCE)
    assert db.scalars(select(orm.UsageEntry.charged)).all() == [False]      # interrupted: not charged (FR-GPU-143)


def test_moving_narrowing_and_cancelling_a_booking(db, db_sessions, seed, labs):  # FR-GPU-084, 138
    node = live_node(db, labs, units=1)
    j = seed.job(labs["NLP"], gpus=1, walltime_h=2)
    seed.commit()
    booking_id, _ = maintenance.book(db, seed.unit_ids(node), NOW + timedelta(hours=1), NOW + timedelta(hours=5),
                                     "rack move", now=NOW)
    assert run_pass(db_sessions, Trigger.TICK, now=NOW).reasons[j] == "MAINTENANCE_WINDOW"

    maintenance.move(db, booking_id, NOW + timedelta(hours=3), NOW + timedelta(hours=5), now=NOW)  # narrowed
    assert j in run_pass(db_sessions, Trigger.MAINTENANCE, now=NOW).placed      # 2 h now fits before it

    maintenance.cancel(db, booking_id)
    assert maintenance.bookings(db, now=NOW) == []
    with pytest.raises(NotFoundError):
        maintenance.cancel(db, booking_id)


@pytest.mark.parametrize("starts, ends, reason, message", [
    (2, 1, "x", "end after it starts"),
    (-3, -2, "x", "end in the future"),
    (1, 2, " ", "needs a reason"),
])
def test_invalid_windows_are_refused(db, labs, starts, ends, reason, message):
    node = live_node(db, labs)
    units = [u for u in db.scalars(select(orm.GpuUnit.id).where(orm.GpuUnit.node_id == node))]
    with pytest.raises(BadRequestError, match=message):
        maintenance.book(db, units, NOW + timedelta(hours=starts), NOW + timedelta(hours=ends), reason, now=NOW)


def test_unknown_units_are_refused(db, labs):
    with pytest.raises(BadRequestError, match="Unknown GPU units"):
        maintenance.preview(db, [9999], NOW, now=NOW)
    with pytest.raises(BadRequestError, match="at least one"):
        maintenance.preview(db, [], NOW, now=NOW)
