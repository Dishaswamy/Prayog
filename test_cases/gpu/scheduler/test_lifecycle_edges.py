"""Edge paths of the job and node lifecycle: refusals, lost races, and an agent that cannot reach the server."""

from datetime import timedelta

import httpx
import pytest

import models as orm
from logic.common.errors import BadRequestError, ConflictError
from logic.gpu.scheduler import nodes, reaper
from logic.gpu.scheduler.config import DEFAULT_CONFIG
from logic.gpu.scheduler.domain import JobState, NodeState, ReleaseReason
from logic.gpu.scheduler.lifecycle import release, request_stop
from logic.gpu.scheduler.reaper import ReapResult
from node_agent.simulated import SimulatedAgent
from test_cases.gpu.scheduler.db_factories import place, start
from test_cases.gpu.scheduler.factories import NOW

pytestmark = pytest.mark.db


def test_a_queued_job_cannot_be_asked_to_stop(db, seed, labs):  # FR-GPU-068 applies to placed jobs only
    j = seed.job(labs["NLP"])
    seed.commit()
    with pytest.raises(ConflictError, match="only a SCHEDULED or RUNNING job"):
        request_stop(db, j, ReleaseReason.CANCELLED, now=NOW)
    assert db.get(orm.Job, j).state == JobState.QUEUED


def test_the_reaper_quietly_loses_a_race_to_an_agent_report(db, db_sessions, seed, labs):  # NFR-GPU-061
    node = seed.node(labs["NLP"])
    j = seed.job(labs["NLP"])
    seed.commit()
    place(db_sessions, j, node)
    start(db_sessions, j, now=NOW)
    release(db, j, ReleaseReason.COMPLETED, now=NOW + timedelta(minutes=5))   # the agent's report got there first

    result = ReapResult()
    reaper._release(db, j, ReleaseReason.NODE_FAILURE, NOW, DEFAULT_CONFIG, result)
    reaper._stop(db, j, ReleaseReason.MAINTENANCE, NOW, DEFAULT_CONFIG, result)

    assert result.released == {} and result.stops_requested == {}
    assert db.get(orm.Job, j).state == JobState.COMPLETED


# ── node action refusals ───────────────────────────────────────────────

def registered(db, labs, units=4):
    reg = nodes.register_node(db, "n-1", labs["NLP"], "shared", False, units, now=NOW)
    return reg.node.id


def test_an_unknown_donation_mode_is_refused(db, labs):
    with pytest.raises(BadRequestError, match="reserved or shared"):
        nodes.register_node(db, "n-x", labs["NLP"], "lent", False, 2, now=NOW)


def test_unit_count_changes_need_the_node_out_of_service(db, db_sessions, seed, labs):  # FR-GPU-170
    node_id = registered(db, labs)
    node = db.get(orm.Node, node_id)
    node.state = NodeState.AVAILABLE
    db.commit()
    with pytest.raises(ConflictError, match="take it out of service"):
        nodes.confirm_unit_count(db, node_id, 2, now=NOW)

    node.state = NodeState.DRAINING
    db.commit()
    with pytest.raises(BadRequestError, match="at least one"):
        nodes.confirm_unit_count(db, node_id, 0, now=NOW)

    node.state = NodeState.AVAILABLE                         # place a job on the top unit, then drain
    db.commit()
    j = seed.job(labs["NLP"], gpus=4)
    seed.commit()
    place(db_sessions, j, node_id)
    node = db.get(orm.Node, node_id)
    db.refresh(node)
    node.state = NodeState.DRAINING
    db.commit()
    with pytest.raises(ConflictError, match="still holds an allocation"):
        nodes.confirm_unit_count(db, node_id, 2, now=NOW)


# ── the agent ──────────────────────────────────────────────────────────

def agent_with(handler, tmp_path):
    client = httpx.Client(base_url="http://server", transport=httpx.MockTransport(handler))
    agent = SimulatedAgent(client, tmp_path, units=2)
    agent.state.update(credential="c", node_id=1)
    return agent


def test_an_agent_keeps_reports_while_the_server_is_unreachable(tmp_path):  # FR-GPU-128
    def down(request):
        raise httpx.ConnectError("no route to server", request=request)

    agent = agent_with(down, tmp_path)
    agent.state["outbox"] = [{"allocation_id": 7, "seq": 1, "outcome": "EXITED", "exit_code": 0}]
    agent._flush_outbox()
    assert len(agent.state["outbox"]) == 1


def test_an_agent_retries_a_report_after_a_server_error(tmp_path):  # FR-GPU-128
    agent = agent_with(lambda request: httpx.Response(503), tmp_path)
    agent.state["outbox"] = [{"allocation_id": 7, "seq": 1, "outcome": "EXITED", "exit_code": 0}]
    agent._flush_outbox()
    assert len(agent.state["outbox"]) == 1


def test_an_agent_drops_its_workspace_when_the_start_is_refused(tmp_path):  # architecture 7.5
    agent = agent_with(lambda request: httpx.Response(409, json={"detail": "do not start"}), tmp_path)
    agent._start("5", {"allocation_id": 5, "walltime_minutes": 60, "unit_indexes": [0], "snapshot_hash": "ab"})
    assert agent.state["running"] == {} and not (tmp_path / "workspaces" / "5").exists()


def test_an_agent_terminates_orphans_the_server_names(tmp_path):  # FR-GPU-136
    def server(request):
        if request.url.path == "/agent/heartbeat":
            return httpx.Response(200, json={"state": "DOWN", "terminate_allocation_ids": [9]})
        return httpx.Response(200, json=[])

    agent = agent_with(server, tmp_path)
    (tmp_path / "workspaces" / "9").mkdir(parents=True)
    agent.state["running"]["9"] = {"started": NOW.isoformat(), "walltime_minutes": 60, "units": [0]}
    agent.step()
    assert agent.state["running"] == {} and not (tmp_path / "workspaces" / "9").exists()


def test_a_booking_that_cannot_be_stored_leaves_nothing_behind(db, labs):
    from sqlalchemy.exc import DataError

    from logic.gpu.scheduler import maintenance

    node_id = registered(db, labs)
    units = [u.id for u in db.query(orm.GpuUnit).filter(orm.GpuUnit.node_id == node_id)]
    with pytest.raises(DataError):                                       # reason longer than its column
        maintenance.book(db, units, NOW + timedelta(hours=1), NOW + timedelta(hours=2), "x" * 300, now=NOW)
    assert maintenance.bookings(db, now=NOW) == []
