"""
Security tests with real tokens and no overridden login (PRAYOG-108; course section 13;
A-18 to A-20). Deliverables/D3/04_Security_Threat_Model.md maps these to the threat model.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import models as orm
from logic.auth.authentication import ALGORITHM, SECRET_KEY, create_access_token

pytestmark = pytest.mark.db


def bearer(user_id, **claims):
    payload = {"sub": str(user_id), "exp": datetime.now(UTC) + timedelta(minutes=30), "ver": 0} | claims
    return {"Authorization": "Bearer " + jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)}


@pytest.fixture
def client(db_sessions):
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
def people(db_sessions, seed, labs):
    ids = {
        "admin": seed.member(labs["NLP"]),
        "alice": seed.member(labs["NLP"]),
        "bob": seed.member(labs["ROBOTICS"]),
        "student": seed.member(labs["COURSE"], course=True, cap=100),
    }
    seed.node(labs["NLP"], units=4)
    seed.node(labs["CENTRAL"], units=4, teaching=True)
    seed.commit()
    with db_sessions() as session:
        session.get(orm.User, ids["admin"]).role = "ADMIN"
        session.commit()
    return ids


def alices_job(client, people, request="a1"):
    r = client.post("/jobs", json={"gpus": 1, "walltime": "01:00", "client_request_id": request},
                    headers=bearer(people["alice"]))
    assert r.status_code == 201, r.text
    return r.json()["request_id"]


# ── authentication ─────────────────────────────────────────────────────

@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer not-a-token"},
    {"Authorization": "Basic abc"},
    {"Authorization": "Bearer " + jwt.encode({"sub": "1"}, "", algorithm="none")},
    {"Authorization": "Bearer " + jwt.encode({"sub": "1", "exp": 4102444800}, "another-key-" * 4, algorithm="HS256")},
])
def test_requests_without_a_valid_token_are_refused(client, headers):
    assert client.get("/jobs", headers=headers).status_code == 401


def test_an_expired_token_is_refused(client, people):
    expired = bearer(people["alice"], exp=datetime.now(UTC) - timedelta(seconds=1))
    assert client.get("/jobs", headers=expired).status_code == 401


def test_a_token_from_before_a_password_reset_is_refused(client, people, db_sessions):
    old = bearer(people["alice"], ver=0)
    with db_sessions() as session:
        session.get(orm.User, people["alice"]).token_version = 1
        session.commit()
    assert client.get("/jobs", headers=old).status_code == 401


def test_a_role_written_into_a_token_is_ignored(client, people):  # the database's role is the one trusted
    forged = bearer(people["student"], role="ADMIN")
    assert client.get("/admin/nodes", headers=forged).status_code == 403


def test_a_logged_out_token_stops_working(client, people, db_sessions):
    with db_sessions() as session:
        token = create_access_token(session.get(orm.User, people["alice"]))
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/jobs", headers=headers).status_code == 200
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/jobs", headers=headers).status_code == 401


# ── authorization abuse (A-18, A-19, A-20) ─────────────────────────────

def test_another_users_job_cannot_be_read_edited_or_cancelled(client, people, db_sessions):  # A-18
    job = alices_job(client, people)
    bob = bearer(people["bob"])
    assert client.get(f"/jobs/{job}", headers=bob).status_code == 404
    assert client.patch(f"/jobs/{job}", json={"gpus": 2}, headers=bob).status_code == 404
    refused, missing = client.post(f"/jobs/{job}/cancel", headers=bob), client.post("/jobs/99999999/cancel", headers=bob)
    assert (refused.status_code, refused.json()) == (missing.status_code, missing.json()) == (404, {"detail": "Job not found."})
    with db_sessions() as session:
        row = session.get(orm.Job, job)
        assert row.state != "CANCELLED" and row.gpus == 1


def test_a_refused_cancel_leaves_an_audit_record(client, people, db_sessions):  # A-18
    job = alices_job(client, people)
    client.post(f"/jobs/{job}/cancel", headers=bearer(people["bob"]))
    with db_sessions() as session:
        events = session.scalars(select(orm.AuditEvent)).all()
    assert [(e.actor, e.operation, e.resource_ids, e.outcome) for e in events] == [
        (str(people["bob"]), "job.cancel", [f"job:{job}"], "REFUSED")
    ]


@pytest.mark.parametrize("who", ["alice", "student"])
@pytest.mark.parametrize("request_", [
    ("get", "/admin/nodes", None),
    ("post", "/admin/nodes", {"name": "x", "owner_lab_id": 1, "donation_mode": "shared", "unit_count": 1}),
    ("get", "/admin/alerts", None),
])
def test_ordinary_users_cannot_reach_the_admin_api(client, people, who, request_):
    method, path, body = request_
    r = client.request(method, path, json=body, headers=bearer(people[who]))
    assert r.status_code == 403, r.text


def test_a_course_member_cannot_choose_a_lab_pool_or_priority(client, people, db_sessions, labs):  # A-19
    r = client.post("/jobs", headers=bearer(people["student"]),
                    json={"gpus": 1, "walltime": "01:00", "client_request_id": "s1", "lab_id": labs["NLP"],
                          "account_type": "research", "priority": "P0", "user_id": people["alice"]})
    assert r.status_code == 201, r.text
    with db_sessions() as session:
        job = session.get(orm.Job, r.json()["request_id"])
        assert (job.user_id, job.lab_id, job.account_type) == (people["student"], labs["COURSE"], "course")


def test_a_course_member_cannot_size_a_request_by_a_research_node(client, people, seed, labs):  # A-19
    seed.node(labs["NLP"], units=32)
    seed.commit()
    r = client.post("/jobs", headers=bearer(people["student"]),
                    json={"gpus": 16, "walltime": "01:00", "client_request_id": "s2"})
    assert r.status_code == 400 and r.json()["code"] == "GPUS_EXCEED_MAX_NODE"


def node_with_credential(client, people, labs, name):
    r = client.post("/admin/nodes", headers=bearer(people["admin"]),
                    json={"name": name, "owner_lab_id": labs["NLP"], "donation_mode": "shared", "unit_count": 4})
    assert r.status_code == 201, r.text
    enrolled = client.post("/agent/enrol", json={"enrolment_token": r.json()["enrolment_token"]})
    return r.json()["node"]["id"], {"X-Node-Credential": enrolled.json()["credential"]}


def test_a_node_cannot_report_for_another_nodes_allocation(client, people, labs, db_sessions):  # A-20
    node_x, x = node_with_credential(client, people, labs, "x")
    _, y = node_with_credential(client, people, labs, "y")
    client.post("/agent/heartbeat", json={"unit_count": 4, "running_allocation_ids": []}, headers=x)
    with db_sessions() as session:
        for node in session.scalars(select(orm.Node).where(orm.Node.id != node_x)):
            node.state = "DOWN"
        session.commit()
    job = alices_job(client, people)
    allocation = client.get("/agent/allocations", headers=x).json()[0]["allocation_id"]

    assert client.post(f"/agent/allocations/{allocation}/acknowledge", headers=y).status_code == 404
    r = client.post(f"/agent/allocations/{allocation}/report", headers=y, json={"seq": 1, "outcome": "EXITED", "exit_code": 0})
    assert r.status_code == 404
    with db_sessions() as session:
        assert session.get(orm.Job, job).state == "SCHEDULED"


def test_user_and_node_credentials_do_not_cross_over(client, people, labs):
    _, node = node_with_credential(client, people, labs, "cross")
    assert client.get("/agent/allocations", headers=bearer(people["alice"])).status_code == 401
    assert client.get("/jobs", headers=node).status_code == 401
    assert client.get("/admin/nodes", headers=node).status_code == 401


def test_a_credential_stops_working_once_stored_only_as_a_hash(client, people, labs, db_sessions):
    node_id, node = node_with_credential(client, people, labs, "hashed")
    with db_sessions() as session:
        row = session.get(orm.Node, node_id)
        assert row.credential_hash != node["X-Node-Credential"]
        assert node["X-Node-Credential"] not in (row.enrolment_token_hash or "")


# ── error handling gives nothing away ──────────────────────────────────

def test_invalid_values_never_echo_internals(client, people):
    r = client.post("/jobs", headers=bearer(people["alice"]),
                    json={"gpus": 1, "walltime": "01:00", "client_request_id": "x\u0000"})
    for leak in ("psycopg2", "sqlalchemy", "Traceback", "SELECT", "INSERT"):
        assert leak not in r.text


def test_sql_in_a_field_is_stored_as_text(client, people, db_sessions):
    attack = "'; DROP TABLE jobs; --"
    r = client.post("/jobs", headers=bearer(people["alice"]),
                    json={"gpus": 1, "walltime": "01:00", "client_request_id": attack, "project_ref": attack})
    assert r.status_code == 201, r.text
    with db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(orm.Job).where(orm.Job.project_ref == attack)) == 1


def test_the_metrics_endpoint_exposes_counts_only(client, people, db_sessions):
    alices_job(client, people)
    body = client.get("/metrics").text
    with db_sessions() as session:
        email = session.get(orm.User, people["alice"]).email
    assert email not in body and "@" not in body
