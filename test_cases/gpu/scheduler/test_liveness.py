"""
A control-plane outage is not a node failure (defect D3-08; FR-GPU-077, NFR-GPU-042, 043).
The reaper judges a node's silence only when the platform was able to hear it.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

import models as orm
from logic.gpu.scheduler.config import DEFAULT_CONFIG, ConfigError, SchedulerConfig
from logic.gpu.scheduler.domain import JobState, NodeState
from logic.gpu.scheduler.liveness import can_judge_silence, stamp, start_stamping
from logic.gpu.scheduler.nodes import heartbeat
from logic.gpu.scheduler.passes import Trigger, run_pass
from logic.gpu.scheduler.reaper import reap
from test_cases.gpu.scheduler.factories import NOW

pytestmark = pytest.mark.db

TIMEOUT = timedelta(seconds=DEFAULT_CONFIG.heartbeat_timeout_seconds)


@pytest.fixture
def running_job(db, db_sessions, seed, labs):
    """A healthy node that last reported at NOW, running a job."""
    node = seed.node(labs["NLP"], units=2)
    job = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    assert job in run_pass(db_sessions, Trigger.TICK, now=NOW).placed
    with db_sessions() as session:
        from logic.gpu.scheduler.lifecycle import mark_running

        mark_running(session, job, now=NOW)
    with db_sessions() as session:
        heartbeat(session, node, 2, [], NOW)
    return node, job


def states(db_sessions, node, job):
    with db_sessions() as session:
        return session.get(orm.Node, node).state, session.get(orm.Job, job).state


def test_an_api_outage_longer_than_the_timeout_marks_no_node_down(db_sessions, running_job):  # D3-08, drill F2
    node, job = running_job
    with db_sessions() as session:
        stamp(session, now=NOW)

    later = NOW + 3 * TIMEOUT                                     # no stamps and no heartbeats since NOW
    with db_sessions() as session:
        result = reap(session, now=later)

    assert result.nodes_down == [] and result.released == {}
    assert states(db_sessions, node, job) == (NodeState.AVAILABLE, JobState.RUNNING)


def test_every_node_gets_a_full_timeout_after_the_platform_returns(db_sessions, running_job):  # D3-08, drill F3
    node, job = running_job
    back = NOW + 3 * TIMEOUT
    with db_sessions() as session:
        stamp(session, now=NOW)
    with db_sessions() as session:
        stamp(session, now=back)                                  # the first stamp after the outage

    with db_sessions() as session:
        result = reap(session, now=back + TIMEOUT / 2)
    assert result.nodes_down == []

    with db_sessions() as session:
        stamp(session, now=back + TIMEOUT / 2)
    with db_sessions() as session:
        heartbeat(session, node, 2, [], back + TIMEOUT / 2)       # the agent gets through
    with db_sessions() as session:
        stamp(session, now=back + TIMEOUT + timedelta(seconds=1))
    with db_sessions() as session:
        result = reap(session, now=back + TIMEOUT + timedelta(seconds=1))
    assert result.nodes_down == []
    assert states(db_sessions, node, job) == (NodeState.AVAILABLE, JobState.RUNNING)


def test_a_node_that_really_died_is_still_detected_after_the_grace(db_sessions, running_job):
    node, job = running_job
    back = NOW + 3 * TIMEOUT
    with db_sessions() as session:
        stamp(session, now=NOW)
    with db_sessions() as session:
        stamp(session, now=back)
    done = back + TIMEOUT + timedelta(seconds=1)
    with db_sessions() as session:
        stamp(session, now=done - timedelta(seconds=1))           # the API is up throughout
    with db_sessions() as session:
        stamp(session, now=done)
    with db_sessions() as session:
        result = reap(session, now=done)

    # The stamps above are farther apart than the gap, so listening restarted; once more, steadily.
    assert result.nodes_down == []
    steady = done
    while steady < done + TIMEOUT + timedelta(seconds=2):
        steady += timedelta(seconds=DEFAULT_CONFIG.platform_stamp_seconds)
        with db_sessions() as session:
            stamp(session, now=steady)
    with db_sessions() as session:
        result = reap(session, now=steady)
    assert result.nodes_down == [node]
    assert states(db_sessions, node, job) == (NodeState.DOWN, JobState.QUEUED)


def test_with_the_platform_steadily_up_a_dead_node_is_detected_on_time(db_sessions, running_job):  # FR-GPU-136
    node, job = running_job
    at = NOW - 2 * TIMEOUT
    with db_sessions() as session:
        stamp(session, now=at)
    while at < NOW + TIMEOUT + timedelta(seconds=2):
        at += timedelta(seconds=DEFAULT_CONFIG.platform_stamp_seconds)
        with db_sessions() as session:
            stamp(session, now=at)
    with db_sessions() as session:
        result = reap(session, now=at)
    assert result.nodes_down == [node]
    assert states(db_sessions, node, job) == (NodeState.DOWN, JobState.QUEUED)


def test_an_outage_does_not_use_up_a_dispatch_attempt(db, db_sessions, seed, labs):  # D3-08, FR-GPU-126
    seed.node(labs["NLP"], units=1)
    job = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    run_pass(db_sessions, Trigger.TICK, now=NOW)
    with db_sessions() as session:
        stamp(session, now=NOW)

    much_later = NOW + timedelta(seconds=10 * DEFAULT_CONFIG.dispatch_ack_timeout_seconds)
    with db_sessions() as session:
        result = reap(session, now=much_later)

    assert result.released == {}
    with db_sessions() as session:
        row = session.get(orm.Job, job)
        assert row.state == JobState.SCHEDULED and row.dispatch_attempts == 0


def test_with_no_stamp_yet_the_reaper_behaves_as_before(db_sessions, running_job):
    with db_sessions() as session:
        assert can_judge_silence(session, NOW) is True


def test_stamping_runs_in_the_background_and_stops(db_sessions):
    stop = start_stamping(db_sessions, SchedulerConfig(platform_stamp_seconds=0.05))
    try:
        for _ in range(100):
            with db_sessions() as session:
                if session.scalar(select(orm.PlatformLiveness.api_seen_at)) is not None:
                    break
            stop.wait(0.05)
        with db_sessions() as session:
            assert session.scalar(select(orm.PlatformLiveness.api_seen_at)) is not None
    finally:
        stop.set()


def test_a_failing_stamp_only_pauses(db_sessions, monkeypatch, caplog):
    from logic.gpu.scheduler import liveness

    calls = []

    def flaky(session, now=None, cfg=DEFAULT_CONFIG):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database went away")
        real(session, now=now, cfg=cfg)

    real = liveness.stamp
    monkeypatch.setattr(liveness, "stamp", flaky)
    stop = liveness.start_stamping(db_sessions, SchedulerConfig(platform_stamp_seconds=0.02))
    try:
        for _ in range(200):
            if len(calls) >= 2:
                break
            stop.wait(0.02)
    finally:
        stop.set()
    assert len(calls) >= 2


def test_the_stamp_must_be_much_faster_than_the_heartbeat_timeout():  # NFR-GPU-062
    with pytest.raises(ConfigError, match="platform_stamp"):
        SchedulerConfig(platform_stamp_seconds=20.0).validate()

