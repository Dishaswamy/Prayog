"""
Deliberate attempts to break the scheduler through its public API (PRAYOG-107):
hostile values, racing clients and a client-supplied claim on someone else's snapshot.
Deliverables/D3/07_Defect_History.md says which of these first failed.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models as orm
from logic.auth.authentication import ALGORITHM, SECRET_KEY

pytestmark = pytest.mark.db


def bearer(user_id, **claims):
    payload = {"sub": str(user_id), "exp": datetime.now(UTC) + timedelta(minutes=30), "ver": 0} | claims
    return {"Authorization": "Bearer " + jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)}


def job_body(request="r1", **extra):
    return {"gpus": 1, "walltime": "01:00", "client_request_id": request, **extra}


def at_once(count, action):
    """Run action(i) from count threads released together."""
    gate = threading.Barrier(count)

    def run(i):
        gate.wait()
        return action(i)

    with ThreadPoolExecutor(count) as pool:
        return list(pool.map(run, range(count)))


@pytest.fixture
def client(db_sessions):
    """The real app with real tokens; a server error is a response here, not an exception."""
    from database import get_db
    from main import app
    from routers.gpu.jobs import get_pass_sessions

    def test_db():
        with db_sessions() as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_pass_sessions] = lambda: db_sessions
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def world(client, db_sessions, seed, labs):
    w = SimpleNamespace(client=client, labs=labs, seed=seed)
    w.admin = seed.member(labs["NLP"])
    w.alice = seed.member(labs["NLP"])
    w.bob = seed.member(labs["ROBOTICS"])
    seed.node(labs["NLP"], units=8)
    seed.commit()
    with db_sessions() as session:
        session.get(orm.User, w.admin).role = "ADMIN"
        session.commit()
    return w


def enrolled_node(w, name="adv-01", units=4):
    r = w.client.post("/admin/nodes", json={"name": name, "owner_lab_id": w.labs["NLP"], "donation_mode": "shared",
                                            "unit_count": units}, headers=bearer(w.admin))
    assert r.status_code == 201, r.text
    enrolled = w.client.post("/agent/enrol", json={"enrolment_token": r.json()["enrolment_token"]})
    assert enrolled.status_code == 200, enrolled.text
    return r.json()["node"]["id"], {"X-Node-Credential": enrolled.json()["credential"]}


# ── hostile values never reach the database as a crash ─────────────────

@pytest.mark.parametrize("field", ["client_request_id", "project_ref", "snapshot_hash"])
def test_a_nul_character_in_a_job_field_is_refused_not_a_server_error(world, field, db):
    r = world.client.post("/jobs", json=job_body() | {field: "a\u0000b"}, headers=bearer(world.alice))
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "INVALID_VALUE"
    assert db.scalar(select(func.count()).select_from(orm.Job)) == 0


def test_a_nul_character_in_a_node_name_is_refused(world):
    r = world.client.post("/admin/nodes", json={"name": "a\u0000", "owner_lab_id": world.labs["NLP"],
                                                "donation_mode": "shared", "unit_count": 1}, headers=bearer(world.admin))
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("units", [10**12, 10**30])
def test_an_absurd_unit_count_in_a_heartbeat_is_refused(world, units):
    _, node = enrolled_node(world)
    r = world.client.post("/agent/heartbeat", json={"unit_count": units, "running_allocation_ids": []}, headers=node)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("report", [
    {"seq": 10**30, "outcome": "EXITED", "exit_code": 0},
    {"seq": 1, "outcome": "EXITED", "exit_code": 10**30},
])
def test_an_out_of_range_agent_report_is_refused_and_the_job_keeps_running(world, db_sessions, report):
    node_id, node = enrolled_node(world)
    world.client.post("/agent/heartbeat", json={"unit_count": 4, "running_allocation_ids": []}, headers=node)
    with db_sessions() as session:                       # only this node can take the job
        for other in session.scalars(select(orm.Node).where(orm.Node.id != node_id)):
            other.state = "DOWN"
        session.commit()
    job_id = world.client.post("/jobs", json=job_body("big"), headers=bearer(world.alice)).json()["request_id"]
    allocation = world.client.get("/agent/allocations", headers=node).json()[0]["allocation_id"]
    assert world.client.post(f"/agent/allocations/{allocation}/acknowledge", headers=node).status_code == 200

    r = world.client.post(f"/agent/allocations/{allocation}/report", json=report, headers=node)

    assert r.status_code == 422, r.text
    with db_sessions() as session:
        assert session.get(orm.Job, job_id).state == "RUNNING"


# ── tokens ─────────────────────────────────────────────────────────────

def test_a_signed_token_without_an_expiry_is_refused(world):
    token = jwt.encode({"sub": str(world.alice), "ver": 0}, SECRET_KEY, algorithm=ALGORITHM)
    assert world.client.get("/jobs", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ── a client-supplied value is not ownership ───────────────────────────

def recorded_snapshot(db_sessions, owner: int) -> str:
    """A snapshot in the ledger that belongs to owner, inserted without its parents' constraints."""
    from sqlalchemy import text

    tag = uuid4().hex
    snapshot_id = str(uuid4())
    with db_sessions() as session:
        session.execute(text("SET session_replication_role = replica"))   # this test only needs the row itself
        session.execute(text(
            "INSERT INTO ml_model_snapshots (snapshot_id, experiment_id, project_id, model_id, run_id, user_id, state,"
            " dependencies, hyperparameters) VALUES (:s, 1, :t, :t, :r, :u, 'RECORDED', '{}', '{}')"
        ), {"s": snapshot_id, "t": tag, "r": abs(hash(tag)) % 10**8, "u": owner})
        session.execute(text("SET session_replication_role = DEFAULT"))
        session.commit()
    return snapshot_id


def test_naming_someone_elses_snapshot_on_a_job_does_not_let_you_rebuild_it(world, db_sessions):
    victims = recorded_snapshot(db_sessions, world.bob)
    attacker = bearer(world.alice)

    assert world.client.post("/jobs", json=job_body("claim", snapshot_hash=victims), headers=attacker).status_code == 201
    r = world.client.post("/rebuilds", headers=attacker,
                          json={"snapshot_hash": victims, "gpus": 1, "walltime": "01:00", "client_request_id": "steal"})

    assert r.status_code == 404, r.text
    with db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(orm.RebuildAttempt)) == 0


# ── racing clients ─────────────────────────────────────────────────────

def test_the_same_request_id_from_many_threads_makes_one_job(world, db_sessions):  # FR-GPU-107, A-24
    headers = bearer(world.alice)
    codes = at_once(8, lambda _: world.client.post("/jobs", json=job_body("dup"), headers=headers).status_code)
    assert sorted(codes) == [200] * 7 + [201]
    with db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(orm.Job)) == 1


def test_members_racing_for_a_shared_cap_cannot_overspend_it(client, db_sessions, seed, labs):  # FR-GPU-104
    members = [seed.member(labs["COURSE"], course=True, cap=10) for _ in range(6)]
    seed.node(labs["CENTRAL"], units=8, teaching=True)
    seed.commit()
    codes = at_once(6, lambda i: client.post("/jobs", json={"gpus": 1, "walltime": "04:00",
                                                            "client_request_id": f"c{i}"},
                                             headers=bearer(members[i])).status_code)
    assert sorted(codes) == [201, 201, 400, 400, 400, 400]       # 4 GPU-hours each against a cap of 10


def test_an_enrolment_token_works_exactly_once_under_a_race(world):  # FR-GPU-110
    r = world.client.post("/admin/nodes", json={"name": "race-01", "owner_lab_id": world.labs["NLP"],
                                                "donation_mode": "shared", "unit_count": 2}, headers=bearer(world.admin))
    token = r.json()["enrolment_token"]
    codes = at_once(6, lambda _: world.client.post("/agent/enrol", json={"enrolment_token": token}).status_code)
    assert sorted(codes) == [200] + [400] * 5


def test_racing_cancels_stop_a_queued_job_once(world, db_sessions):  # FR-GPU-065
    with db_sessions() as session:                       # nothing can place it, so it stays QUEUED
        for node in session.scalars(select(orm.Node)):
            node.state = "DOWN"
        session.commit()
    headers = bearer(world.alice)
    job_id = world.client.post("/jobs", json=job_body("c"), headers=headers).json()["request_id"]
    responses = at_once(6, lambda _: world.client.post(f"/jobs/{job_id}/cancel", headers=headers))
    assert all(r.status_code in (200, 409) for r in responses), [r.text for r in responses]
    with db_sessions() as session:
        assert session.get(orm.Job, job_id).state == "CANCELLED"


# ── the database goes away (NFR-GPU-043) ───────────────────────────────

def test_a_submission_during_a_database_outage_gets_a_retryable_answer(world):
    from database import get_db
    from main import app

    dead = sessionmaker(bind=create_engine("postgresql+psycopg2://u:p@127.0.0.1:1/none",
                                           connect_args={"connect_timeout": 1}))

    def no_database():
        with dead() as session:
            yield session

    app.dependency_overrides[get_db] = no_database
    r = world.client.post("/jobs", json=job_body("outage"), headers=bearer(world.alice))
    assert r.status_code == 503, r.text
    assert r.headers["Retry-After"]
    assert r.json()["code"] == "DATABASE_UNAVAILABLE"
    assert "127.0.0.1" not in r.text and "psycopg2" not in r.text


# ── a pass that lost a race stops paying for its stale snapshot (PRAYOG-109) ───

def stale_pass(job_sizes, node_count, monkeypatch, status="LOST_CLAIM_RACE"):
    """A pass whose every claim fails as if another pass got there first. Returns the claims it tried."""
    from logic.gpu.scheduler import passes
    from logic.gpu.scheduler.claim import ClaimOutcome, ClaimStatus
    from logic.gpu.scheduler.scoring import NO_USAGE, score_job
    from test_cases.gpu.scheduler.factories import NOW, job, node

    tried = []

    def losing_claim(session, job_id, node_id, *args, **kwargs):
        tried.append((job_id, node_id))
        return ClaimOutcome(ClaimStatus(status))

    monkeypatch.setattr(passes, "claim", losing_claim)
    jobs = [job(job_id=i + 1, gpus=g) for i, g in enumerate(job_sizes)]
    nodes = [node(node_id=100 + i, units=4) for i in range(node_count)]
    breakdowns = {j.job_id: score_job(j, NOW, NO_USAGE) for j in jobs}
    passes._Pass(None, "stale", "test", NOW, jobs, nodes, breakdowns).run()
    return tried


def test_a_lost_race_is_not_retried_for_every_job_on_every_node(monkeypatch):
    assert len(stale_pass([1] * 30, 3, monkeypatch)) == 3


def test_a_lost_race_for_two_gpus_still_lets_a_one_gpu_job_try():
    from logic.gpu.scheduler import passes
    from logic.gpu.scheduler.claim import ClaimStatus
    from logic.gpu.scheduler.eligibility import is_eligible
    from test_cases.gpu.scheduler.factories import NOW, job, node

    two, one = job(job_id=1, gpus=2), job(job_id=2, gpus=1)
    state = passes._Pass(None, "stale", "test", NOW, [two, one], [node(node_id=100, units=4)], {})
    state._learn(100, two, ClaimStatus.LOST_CLAIM_RACE)
    assert not is_eligible(two, state.nodes[100], NOW)
    assert is_eligible(one, state.nodes[100], NOW)


def test_a_node_that_left_available_is_not_retried(monkeypatch):
    assert len(stale_pass([1] * 30, 3, monkeypatch, status="NODE_UNAVAILABLE")) == 3


# ── a burst of triggers does not stack up passes (PRAYOG-109) ──────────

def test_a_burst_of_triggers_runs_one_pass_plus_one_owed(monkeypatch):
    from logic.gpu.scheduler import passes

    entered, release_first = threading.Event(), threading.Event()
    runs = []

    def slow_pass(sessions, trigger, cfg=None):
        runs.append(trigger)
        if len(runs) == 1:
            entered.set()
            release_first.wait(5)

    monkeypatch.setattr(passes, "run_pass", slow_pass)
    first = threading.Thread(target=passes.trigger_pass, args=(None, "first"))
    first.start()
    assert entered.wait(5)
    for i in range(20):
        passes.trigger_pass(None, f"burst-{i}")                   # returns at once: the running pass owes one more
    assert runs == ["first"]
    release_first.set()
    first.join(5)
    assert len(runs) == 2                                         # the owed pass sees everything the burst committed


def test_a_failed_pass_does_not_stop_later_triggers(monkeypatch):
    from logic.gpu.scheduler import passes

    calls = []

    def failing_once(sessions, trigger, cfg=None):
        calls.append(trigger)
        if len(calls) == 1:
            raise RuntimeError("database went away")

    monkeypatch.setattr(passes, "run_pass", failing_once)
    with pytest.raises(RuntimeError):
        passes.trigger_pass(None, "one")
    passes.trigger_pass(None, "two")
    assert calls == ["one", "two"]


# ── a broken release hook cannot hold GPUs hostage (Steward failure, FR-GPU-146) ───

def test_a_release_hook_that_raises_does_not_stop_the_release(db_sessions, seed, labs, monkeypatch):
    from logic.gpu.scheduler import lifecycle
    from logic.gpu.scheduler.domain import JobState, ReleaseReason
    from logic.gpu.scheduler.passes import Trigger, run_pass

    def broken_hook(*args, **kwargs):
        raise RuntimeError("the model adapter returned nonsense")

    monkeypatch.setattr(lifecycle, "RELEASE_HOOKS", [broken_hook])
    node = seed.node(labs["NLP"], units=1)
    job = seed.job(labs["NLP"], gpus=1)
    seed.commit()
    assert job in run_pass(db_sessions, Trigger.TICK).placed

    with db_sessions() as session:
        lifecycle.mark_running(session, job)
    with db_sessions() as session:
        result = lifecycle.release(session, job, ReleaseReason.COMPLETED)

    assert result.job_state == JobState.COMPLETED
    with db_sessions() as session:
        assert session.scalar(select(orm.GpuUnit.current_allocation_id).where(orm.GpuUnit.node_id == node)) is None
