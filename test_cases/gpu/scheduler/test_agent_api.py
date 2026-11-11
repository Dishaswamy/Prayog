"""
The whole job lifecycle through the HTTP API, with the simulated node agent as a real
client: register a node, enrol its agent, submit, run, finish, cancel, stop at walltime,
and the admin operations around them (A-04, A-07, A-11, A-12; FR-GPU-059 to 069).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import models as orm
from logic.gpu.scheduler.config import SchedulerConfig
from node_agent.simulated import SimulatedAgent, SimulatedExecutor, utc_now

pytestmark = pytest.mark.db


class Clock:
    """The agent's clock, moved by hand."""
    def __init__(self):
        self.now = datetime(2026, 10, 14, 9, 0)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def world(db_sessions, seed, labs, tmp_path):
    from database import get_db
    from logic.auth.dependencies import get_current_user
    from main import app
    from routers.gpu.jobs import get_pass_sessions, get_scheduler_config

    def test_db():
        with db_sessions() as session:
            yield session

    w = SimpleNamespace(client=TestClient(app), user=None, clock=Clock(), tmp=tmp_path, labs=labs, seed=seed)
    w.admin = SimpleNamespace(id=seed.member(labs["NLP"]), role="ADMIN")
    w.alice = SimpleNamespace(id=seed.member(labs["NLP"]), role="RESEARCHER")
    seed.commit()
    w.user = w.alice
    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_pass_sessions] = lambda: db_sessions
    app.dependency_overrides[get_current_user] = lambda: w.user               # login itself is Disha's
    app.dependency_overrides[get_scheduler_config] = lambda: SchedulerConfig()
    yield w
    app.dependency_overrides.clear()


def as_admin(w):
    w.user = w.admin


def as_alice(w):
    w.user = w.alice


def new_node(w, units=4, name="nlp-01", run_seconds=60.0, exit_code=0, agent_dir="agent"):
    """Register a node as admin, start its agent, and let it heartbeat once."""
    as_admin(w)
    r = w.client.post("/admin/nodes", json={"name": name, "owner_lab_id": w.labs["NLP"], "donation_mode": "shared",
                                            "unit_count": units})
    assert r.status_code == 201, r.text
    agent = SimulatedAgent(w.client, w.tmp / agent_dir, units, SimulatedExecutor(run_seconds, exit_code), w.clock)
    agent.enrol(r.json()["enrolment_token"])
    agent.step()
    as_alice(w)
    return r.json()["node"]["id"], agent


def submit(w, gpus=2, walltime="04:00", request="r1"):
    r = w.client.post("/jobs", json={"gpus": gpus, "walltime": walltime, "client_request_id": request})
    assert r.status_code == 201, r.text
    return r.json()["request_id"]


def status(w, job_id):
    return w.client.get(f"/jobs/{job_id}").json()


# ── the core workflow ──────────────────────────────────────────────────

def test_a_job_goes_the_whole_way(world, db):  # A-04, FR-GPU-059 to 064, 069
    w = world
    node_id, agent = new_node(w)
    as_admin(w)
    assert w.client.get("/admin/nodes").json()[0]["state"] == "AVAILABLE"      # first heartbeat matched
    as_alice(w)

    j = submit(w)
    assert status(w, j)["state"] == "SCHEDULED"
    agent.step()                                                                # pulls, acknowledges, starts
    s = status(w, j)
    assert (s["state"], s["node_id"], s["unit_indexes"]) == ("RUNNING", node_id, [0, 1])
    assert s["started_at"] is not None and s["walltime_ends_at"] is not None

    w.clock.advance(seconds=61)
    agent.step()                                                                # the process exits 0, reports
    s = status(w, j)
    assert (s["state"], s["end_state"], s["exit_code"], s["node_id"]) == ("COMPLETED", "COMPLETED", 0, None)
    assert db.scalars(select(orm.UsageEntry.charged)).all() == [True]
    assert agent.state["running"] == {} and not (w.tmp / "agent" / "workspaces" / str(j)).exists()


def test_a_failing_process_ends_the_job_failed(world):
    w = world
    _, agent = new_node(w, exit_code=2)
    j = submit(w)
    agent.step()
    w.clock.advance(seconds=61)
    agent.step()
    s = status(w, j)
    assert (s["state"], s["exit_code"]) == ("FAILED", 2)


def test_cancelling_a_running_job_frees_its_gpus_only_after_the_node_confirms(world):  # A-11, FR-GPU-068, 069
    w = world
    _, agent = new_node(w, units=2, run_seconds=3600)
    j = submit(w)
    waiting = submit(w, request="r2")
    agent.step()

    r = w.client.post(f"/jobs/{j}/cancel")
    assert r.status_code == 200 and r.json()["stop_pending"] is True and r.json()["state"] == "RUNNING"
    assert status(w, j)["stop_reason"] == "CANCELLED"
    assert status(w, waiting)["state"] == "QUEUED"                             # GPUs still held

    agent.step()                                                                # stops it and reports
    assert status(w, j)["state"] == "CANCELLED"
    assert status(w, waiting)["state"] == "SCHEDULED"                          # freed GPUs used at once


def test_the_agent_enforces_walltime_itself(world):  # A-12, FR-GPU-134
    w = world
    _, agent = new_node(w, run_seconds=10_000)
    j = submit(w, walltime="00:01")
    agent.step()
    w.clock.advance(seconds=61)
    agent.step()
    assert status(w, j)["state"] == "WALLTIME_EXCEEDED"


def test_a_restarted_agent_never_starts_a_run_twice(world, db):  # A-07, FR-GPU-061; NFR-GPU-019
    w = world
    _, agent = new_node(w, run_seconds=3600)
    j = submit(w)
    agent.step()
    restarted = SimulatedAgent(w.client, w.tmp / "agent", 4, SimulatedExecutor(3600), w.clock)   # same state dir
    restarted.step()
    restarted.step()
    assert status(w, j)["state"] == "RUNNING"
    assert len(db.scalars(select(orm.Run).where(orm.Run.job_id == j)).all()) == 1


def test_a_cancel_before_the_start_means_the_agent_never_starts(world, db):  # architecture 7.5
    w = world
    _, agent = new_node(w)
    j = submit(w)
    assert w.client.post(f"/jobs/{j}/cancel").json()["state"] == "CANCELLED"
    agent.step()
    assert agent.state["running"] == {}
    assert db.scalars(select(orm.Run)).all() == []


# ── node identity and scoping (FR-GPU-129) ─────────────────────────────

def test_an_unknown_node_credential_is_refused(world):
    w = world
    assert w.client.get("/agent/allocations", headers={"X-Node-Credential": "nope"}).status_code == 401
    assert w.client.get("/agent/allocations").status_code == 401


def test_a_node_cannot_touch_another_nodes_allocation(world):  # FR-GPU-129
    w = world
    _, first = new_node(w, name="nlp-01", agent_dir="a1")
    _, second = new_node(w, name="nlp-02", agent_dir="a2")
    submit(w, gpus=4)                                                          # best fit: whichever node
    first.step()
    second.step()
    mine, theirs = (first, second) if first.state["running"] else (second, first)
    allocation = next(iter(mine.state["running"]))
    r = w.client.post(f"/agent/allocations/{allocation}/report",
                      headers={"X-Node-Credential": theirs.state["credential"]},
                      json={"seq": 99, "outcome": "EXITED", "exit_code": 0})
    assert r.status_code == 404


def test_a_decommissioned_nodes_credential_stops_working(world, db_sessions):  # FR-GPU-115
    from logic.gpu.scheduler.reaper import reap

    w = world
    node_id, agent = new_node(w)
    as_admin(w)
    assert w.client.post(f"/admin/nodes/{node_id}/decommission", json={"reason": "retired"}).status_code == 200
    with db_sessions() as s:
        reap(s)
    assert w.client.post("/agent/heartbeat", headers={"X-Node-Credential": agent.state["credential"]},
                         json={"unit_count": 4}).status_code == 401


# ── admin operations ───────────────────────────────────────────────────

def test_admin_routes_need_the_admin_role(world):  # FR-GPU-175
    w = world
    for method, path in [("get", "/admin/nodes"), ("get", "/admin/alerts"), ("get", "/admin/maintenance"),
                         ("post", "/admin/maintenance/preview")]:
        response = getattr(w.client, method)(path, **({"json": {"reason": "x"}} if method == "post" else {}))
        assert response.status_code == 403, path


def test_drain_and_return_through_the_api(world):  # FR-GPU-168, 169
    w = world
    node_id, agent = new_node(w)
    as_admin(w)
    r = w.client.post(f"/admin/nodes/{node_id}/drain", json={"mode": "soft", "reason": "fan"})
    assert (r.json()["state"], r.json()["drain_mode"], r.json()["state_reason"]) == ("DRAINING", "soft", "fan")
    assert w.client.post(f"/admin/nodes/{node_id}/return").json()["return_requested"] is True
    agent.step()
    assert w.client.get("/admin/nodes").json()[0]["state"] == "AVAILABLE"
    assert w.client.post(f"/admin/nodes/{node_id}/escalate", json={"reason": "x"}).status_code == 409


def test_unit_count_alerts_through_the_api(world):  # FR-GPU-110, 170, 174
    w = world
    as_admin(w)
    r = w.client.post("/admin/nodes", json={"name": "vis-01", "owner_lab_id": w.labs["NLP"],
                                            "donation_mode": "shared", "unit_count": 4})
    agent = SimulatedAgent(w.client, w.tmp / "bad", 3, SimulatedExecutor(), w.clock)        # sees only 3
    agent.enrol(r.json()["enrolment_token"])
    agent.step()
    [alert] = w.client.get("/admin/alerts").json()
    assert alert["kind"] == "UNIT_COUNT_MISMATCH"
    node_id = r.json()["node"]["id"]
    assert w.client.post(f"/admin/nodes/{node_id}/unit-count", json={"unit_count": 3}).status_code == 200
    agent.step()
    assert w.client.get("/admin/nodes").json()[0]["state"] == "AVAILABLE"
    assert w.client.get("/admin/alerts").json() == []
    assert w.client.post(f"/admin/alerts/{alert['id']}/acknowledge", json={"reason": "x"}).status_code == 409


def test_maintenance_through_the_api(world, db):  # FR-GPU-081, 083, 084, 138
    w = world
    node_id, agent = new_node(w, run_seconds=10_000)
    submit(w, gpus=1)
    agent.step()
    as_admin(w)
    units = list(db.scalars(select(orm.GpuUnit.id).where(orm.GpuUnit.node_id == node_id).order_by(orm.GpuUnit.id)))
    soon = (utc_now() + timedelta(hours=1)).isoformat()
    later = (utc_now() + timedelta(hours=2)).isoformat()

    p = w.client.post("/admin/maintenance/preview", json={"unit_ids": units, "starts_at": soon}).json()
    assert len(p["collisions"]) == 1
    r = w.client.post("/admin/maintenance", json={"unit_ids": units, "starts_at": soon, "ends_at": later,
                                                  "reason": "BIOS update"})
    assert r.status_code == 409 and r.json()["code"] == "MAINTENANCE_COLLISION" and len(r.json()["collisions"]) == 1
    r = w.client.post("/admin/maintenance", json={"unit_ids": units, "starts_at": soon, "ends_at": later,
                                                  "reason": "BIOS update", "confirm": True})
    assert r.status_code == 201
    booking = r.json()["booking_id"]
    assert [b["booking_id"] for b in w.client.get("/admin/maintenance").json()] == [booking]
    moved = w.client.patch(f"/admin/maintenance/{booking}", json={"starts_at": soon, "ends_at": later})
    assert moved.status_code == 409                                            # still collides, not confirmed
    far = (utc_now() + timedelta(days=2)).isoformat()
    farther = (utc_now() + timedelta(days=2, hours=1)).isoformat()
    moved = w.client.patch(f"/admin/maintenance/{booking}", json={"starts_at": far, "ends_at": farther})
    assert moved.status_code == 200 and moved.json()["collisions"] == []       # moved past the clear-by time
    assert w.client.delete(f"/admin/maintenance/{booking}").status_code == 204
    assert w.client.get("/admin/maintenance").json() == []


def test_maintenance_times_with_a_timezone_are_stored_as_utc(world, db):
    w = world
    node_id, _ = new_node(w)
    as_admin(w)
    unit = db.scalar(select(orm.GpuUnit.id).where(orm.GpuUnit.node_id == node_id).limit(1))
    r = w.client.post("/admin/maintenance", json={"unit_ids": [unit], "starts_at": "2030-01-01T10:00:00+05:30",
                                                  "ends_at": "2030-01-01T12:00:00+05:30", "reason": "ups"})
    assert r.status_code == 201
    assert db.scalar(select(orm.MaintenanceWindow.starts_at)) == datetime(2030, 1, 1, 4, 30)


def test_escalate_and_abort_through_the_api(world):  # FR-GPU-171
    w = world
    node_id, _ = new_node(w)
    as_admin(w)
    w.client.post(f"/admin/nodes/{node_id}/drain", json={"mode": "hard", "reason": "dead GPU"})
    assert w.client.post(f"/admin/nodes/{node_id}/escalate", json={"reason": "beyond repair"}).json()["state"] == (
        "DECOMMISSIONING")
    r = w.client.post(f"/admin/nodes/{node_id}/abort-decommission", json={"reason": "part arrived"})
    assert (r.json()["state"], r.json()["state_reason"]) == ("DRAINING", "part arrived")


def test_a_repeated_report_is_acknowledged_but_not_applied_twice(world):  # FR-GPU-128
    w = world
    _, agent = new_node(w)
    j = submit(w)
    agent.step()
    allocation = next(iter(agent.state["running"]))
    headers = {"X-Node-Credential": agent.state["credential"]}
    body = {"seq": 1, "outcome": "EXITED", "exit_code": 0}
    first = w.client.post(f"/agent/allocations/{allocation}/report", headers=headers, json=body)
    again = w.client.post(f"/agent/allocations/{allocation}/report", headers=headers, json=body)
    assert first.json() == {"applied": True, "job_state": "COMPLETED"}
    assert again.json() == {"applied": False, "job_state": None}
    assert status(w, j)["state"] == "COMPLETED"


def test_status_shows_a_planned_maintenance_stop(world, db):  # FR-GPU-109
    w = world
    node_id, agent = new_node(w, run_seconds=10_000)
    j = submit(w, gpus=1, walltime="08:00")
    agent.step()
    as_admin(w)
    unit = db.scalar(select(orm.GpuUnit.id).where(orm.GpuUnit.node_id == node_id, orm.GpuUnit.unit_index == 0))
    soon = utc_now() + timedelta(hours=1)
    w.client.post("/admin/maintenance", json={"unit_ids": [unit], "starts_at": soon.isoformat(),
                                              "ends_at": (soon + timedelta(hours=1)).isoformat(),
                                              "reason": "cooling", "confirm": True})
    as_alice(w)
    s = status(w, j)
    assert s["planned_stop_reason"] == "cooling"
    assert datetime.fromisoformat(s["planned_stop_at"]) == soon


def test_another_users_job_cannot_be_cancelled(world, seed, labs):  # FR-GPU-065
    w = world
    new_node(w)
    j = submit(w)
    w.user = SimpleNamespace(id=seed.member(labs["NLP"]), role="RESEARCHER")
    seed.commit()
    assert w.client.post(f"/jobs/{j}/cancel").status_code == 404
