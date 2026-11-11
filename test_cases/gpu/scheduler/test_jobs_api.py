"""
POST /jobs and GET /jobs/{id} end to end: request, validation, the pass the
submission triggers (no tick runs in tests), and the status the owner sees.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import models as orm
from logic.gpu.scheduler.config import SchedulerConfig
from logic.gpu.scheduler.domain import NodeState

pytestmark = pytest.mark.db


class Api:
    def __init__(self, client: TestClient):
        self.client = client
        self.user_id: int | None = None
        self.config = SchedulerConfig()

    def submit(self, gpus=2, walltime="04:00", request="req-1", **extra):
        body = {"gpus": gpus, "walltime": walltime, "client_request_id": request, **extra}
        return self.client.post("/jobs", json=body)

    def status(self, job_id):
        return self.client.get(f"/jobs/{job_id}")


@pytest.fixture
def api(db_sessions):
    from database import get_db
    from logic.auth.dependencies import get_current_user
    from main import app
    from routers.gpu.jobs import get_pass_sessions, get_scheduler_config

    def test_db():
        with db_sessions() as session:
            yield session

    api = Api(TestClient(app))
    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_pass_sessions] = lambda: db_sessions
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=api.user_id)   # login is Disha's, tested there
    app.dependency_overrides[get_scheduler_config] = lambda: api.config
    yield api
    app.dependency_overrides.clear()


def job_count(db):
    return db.scalar(select(func.count()).select_from(orm.Job))


def rejected(response, code, status=400):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    return response.json()["detail"]


# ── answered on the spot ───────────────────────────────────────────────

def test_valid_request_is_answered_in_process_then_scheduled(api, seed, labs):  # A-81, A-04, A-62 (submit), FR-GPU-017
    seed.node(labs["NLP"], units=4)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()

    response = api.submit(gpus=2, walltime="04:00")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "IN_PROCESS"
    assert body["message"] == f"Request submitted. Request ID: {body['request_id']}. Status: IN PROCESS"
    # The pass the submission triggered has already run: no tick in tests.
    status = api.status(body["request_id"]).json()
    assert status["state"] == "SCHEDULED" and status["queue_reason"] is None
    assert (status["node_name"], status["unit_indexes"], status["gpus"], status["walltime"]) == (
        "node-1", [0, 1], 2, "04:00"                                       # FR-GPU-022
    )


def test_impossible_request_is_rejected_on_the_spot_and_stores_nothing(api, db, seed, labs):  # A-81, A-01, FR-GPU-015
    seed.node(labs["NLP"], units=8)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()

    detail = rejected(api.submit(gpus=10), "GPUS_EXCEED_MAX_NODE")

    assert detail == "Requested 10 GPUs, but the largest node you can use has 8."
    assert job_count(db) == 0


def test_walltime_over_the_ceiling_is_rejected(api, db, seed, labs):  # A-02, FR-GPU-014
    seed.node(labs["NLP"], units=8)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()

    detail = rejected(api.submit(walltime="60:00"), "WALLTIME_EXCEEDS_MAX")

    assert detail == "Walltime 60:00 exceeds the maximum of 48:00."
    assert job_count(db) == 0


def test_ceilings_are_configuration(api, seed, labs):  # A-78, NFR-GPU-053
    seed.node(labs["NLP"], units=8)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    api.config = SchedulerConfig(max_walltime_minutes=3 * 60)

    assert "maximum of 03:00" in rejected(api.submit(walltime="04:00"), "WALLTIME_EXCEEDS_MAX")


@pytest.mark.parametrize("gpus, walltime, code", [
    (0, "04:00", "INVALID_GPUS"),
    ("two", "04:00", "INVALID_GPUS"),
    (None, "04:00", "INVALID_GPUS"),
    (2, "4 hours", "INVALID_WALLTIME"),
    (2, 240, "INVALID_WALLTIME"),
])
def test_malformed_values_get_a_rejection_code(api, db, seed, labs, gpus, walltime, code):  # FR-GPU-016, A-70
    seed.node(labs["NLP"], units=8)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()

    rejected(api.submit(gpus=gpus, walltime=walltime), code)
    assert job_count(db) == 0


def test_user_without_gpu_authorization_is_refused(api, db, seed, labs):  # FR-GPU-106
    seed.node(labs["NLP"], units=8)
    api.user_id = seed._user_in(labs["NLP"])                        # a user with no scheduling context
    seed.commit()

    rejected(api.submit(), "AUTHORIZATION_INACTIVE", status=403)
    assert job_count(db) == 0


def test_a_retried_submission_returns_the_same_request(api, db, seed, labs):  # A-24, FR-GPU-107
    seed.node(labs["NLP"], units=4)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()

    first = api.submit(request="cli-retry-1").json()
    retry = api.submit(request="cli-retry-1")

    assert retry.status_code == 200
    assert retry.json()["request_id"] == first["request_id"]
    assert retry.json()["status"] == "SCHEDULED"                    # the original's current state
    assert job_count(db) == 1


# ── what counts as the largest reachable node (FR-GPU-013) ─────────────

def test_research_accounts_cannot_size_requests_by_the_teaching_pool(api, seed, labs):  # A-61
    seed.node(labs["CENTRAL"], units=8, teaching=True)
    seed.node(labs["VISION"], units=4)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()

    assert "has 4" in rejected(api.submit(gpus=6), "GPUS_EXCEED_MAX_NODE")


def test_course_accounts_are_limited_to_the_teaching_pool(api, seed, labs):  # A-19
    seed.node(labs["CENTRAL"], units=4, teaching=True)
    seed.node(labs["VISION"], units=8)
    api.user_id = seed.member(labs["COURSE"], course=True)
    seed.commit()

    assert "has 4" in rejected(api.submit(gpus=6), "GPUS_EXCEED_MAX_NODE")
    assert api.submit(gpus=4, request="fits").status_code == 201


def test_another_labs_reserved_node_does_not_count(api, seed, labs):  # FR-GPU-033
    seed.node(labs["ROBOTICS"], units=8, shared=False)
    seed.node(labs["NLP"], units=2, shared=False)
    nlp_member = seed.member(labs["NLP"])
    robotics_member = seed.member(labs["ROBOTICS"])
    seed.commit()

    api.user_id = nlp_member
    assert "has 2" in rejected(api.submit(gpus=6), "GPUS_EXCEED_MAX_NODE")
    api.user_id = robotics_member
    assert api.submit(gpus=6).status_code == 201


def test_a_down_node_still_counts_but_a_decommissioned_one_does_not(api, seed, labs):  # FR-GPU-013, FR-GPU-035
    seed.node(labs["NLP"], units=8, state=NodeState.DOWN)
    seed.node(labs["NLP"], units=16, state=NodeState.DECOMMISSIONED)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()

    assert "has 8" in rejected(api.submit(gpus=10), "GPUS_EXCEED_MAX_NODE")
    accepted = api.submit(gpus=8, request="waits-for-repair").json()
    status = api.status(accepted["request_id"]).json()
    assert (status["state"], status["queue_reason"]) == ("QUEUED", "NO_ELIGIBLE_NODE")   # temporary: queued


# ── status ─────────────────────────────────────────────────────────────

def test_status_of_a_queued_job_shows_its_reason(api, seed, labs):  # A-03, A-08, FR-GPU-021
    seed.node(labs["NLP"], units=4)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    api.submit(gpus=3, request="big")

    second = api.submit(gpus=2, request="waits").json()

    status = api.status(second["request_id"]).json()
    assert (status["state"], status["queue_reason"]) == ("QUEUED", "INSUFFICIENT_FREE_UNITS")
    assert (status["node_id"], status["unit_indexes"]) == (None, [])


def test_another_users_job_is_not_found(api, seed, labs):  # NFR-GPU-030
    seed.node(labs["NLP"], units=4)
    owner, other = seed.member(labs["NLP"]), seed.member(labs["NLP"])
    seed.commit()
    api.user_id = owner
    job_id = api.submit().json()["request_id"]

    api.user_id = other
    assert api.status(job_id).status_code == 404
    assert api.status(99999).status_code == 404


def test_production_wiring_uses_the_app_database_and_the_loaded_config():  # NFR-GPU-053, 062
    from database import SessionLocal
    from logic.gpu.scheduler.config import load_config
    from routers.gpu.jobs import ACTIVE_CONFIG, get_pass_sessions, get_scheduler_config

    assert get_pass_sessions() is SessionLocal
    assert get_scheduler_config() is ACTIVE_CONFIG == load_config()


# ── edit ───────────────────────────────────────────────────────────────

def test_shrinking_a_queued_job_lets_the_edit_triggered_pass_place_it(api, seed, labs):  # FR-GPU-070, A-62 (edit)
    seed.node(labs["NLP"], units=3)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    api.submit(gpus=2, request="first")                                       # takes 2 of the 3 units
    waiting = api.submit(gpus=2, request="waits").json()["request_id"]
    assert api.status(waiting).json()["state"] == "QUEUED"

    response = api.client.patch(f"/jobs/{waiting}", json={"gpus": 1, "walltime": "01:00"})

    assert response.status_code == 200, response.text
    assert (response.json()["gpus"], response.json()["walltime"]) == (1, "01:00")
    status = api.status(waiting).json()                                       # the edit's pass has run
    assert (status["state"], status["unit_indexes"]) == ("SCHEDULED", [2])


def test_editing_a_placed_job_is_refused(api, seed, labs):  # FR-GPU-131
    seed.node(labs["NLP"], units=2)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    j = api.submit(gpus=2).json()["request_id"]

    rejected(api.client.patch(f"/jobs/{j}", json={"gpus": 1}), "JOB_NOT_QUEUED", status=409)


def test_an_invalid_edit_gets_a_rejection_code(api, seed, labs):  # FR-GPU-130
    seed.node(labs["NLP"], units=2, state=NodeState.DOWN)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    j = api.submit(gpus=2).json()["request_id"]

    rejected(api.client.patch(f"/jobs/{j}", json={"walltime": "99:00"}), "WALLTIME_EXCEEDS_MAX")
    rejected(api.client.patch(f"/jobs/{j}", json={"gpus": None}), "INVALID_GPUS")
    assert api.status(j).json()["walltime"] == "04:00"


# ── until when, quota, metrics ─────────────────────────────────────────

def test_status_shows_until_when_own_lab_units_are_busy(api, db, db_sessions, seed, labs):  # FR-GPU-108, A-08
    from logic.gpu.scheduler.lifecycle import mark_running

    seed.node(labs["NLP"], units=2)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    first = api.submit(gpus=2, walltime="03:00", request="first").json()["request_id"]
    with db_sessions() as s:
        mark_running(s, first)
    started = db.scalar(select(orm.Allocation.acknowledged_at))
    waiting = api.submit(gpus=2, request="second").json()["request_id"]

    status = api.status(waiting).json()

    assert status["queue_reason"] == "INSUFFICIENT_FREE_UNITS"
    expected = started + timedelta(hours=3, seconds=api.config.walltime_completion_bound_seconds)
    assert datetime.fromisoformat(status["p0_free_at"]) == expected


def test_status_of_a_finished_job_shows_no_placement_or_wait(api, db, db_sessions, seed, labs):  # FR-GPU-020
    from logic.gpu.scheduler.domain import ReleaseReason
    from logic.gpu.scheduler.lifecycle import mark_running, release

    seed.node(labs["NLP"], units=2)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    j = api.submit(gpus=1).json()["request_id"]
    with db_sessions() as s:
        mark_running(s, j)
    with db_sessions() as s:
        release(s, j, ReleaseReason.COMPLETED)

    status = api.status(j).json()

    assert status["state"] == "COMPLETED"
    assert (status["queue_reason"], status["p0_free_at"], status["node_id"], status["unit_indexes"]) == (
        None, None, None, [])


def test_a_course_job_has_no_own_lab_wait_time(api, seed, labs):  # FR-GPU-108 (P0 only)
    seed.node(labs["CENTRAL"], units=1, teaching=True)
    api.user_id = seed.member(labs["COURSE"], course=True)
    seed.commit()
    api.submit(gpus=1, request="first")
    waiting = api.submit(gpus=1, request="second").json()["request_id"]

    status = api.status(waiting).json()

    assert (status["state"], status["queue_reason"], status["p0_free_at"]) == (
        "QUEUED", "INSUFFICIENT_FREE_UNITS", None)


def test_quota_endpoint_for_a_course_account(api, seed, labs):  # FR-GPU-178
    seed.node(labs["CENTRAL"], units=4, teaching=True)
    api.user_id = seed.member(labs["COURSE"], course=True, cap=10)
    seed.commit()
    api.submit(gpus=1, walltime="02:00")

    body = api.client.get("/jobs/quota").json()

    assert (body["cap_gpu_hours"], body["committed_gpu_hours"], body["remaining_gpu_hours"]) == (10, 2, 8)


def test_quota_endpoint_refuses_accounts_without_a_cap(api, seed, labs):  # FR-GPU-178
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    assert api.client.get("/jobs/quota").status_code == 404


def test_over_cap_submission_is_rejected_through_the_api(api, db, seed, labs):  # A-25
    seed.node(labs["CENTRAL"], units=4, teaching=True)
    api.user_id = seed.member(labs["COURSE"], course=True, cap=3)
    seed.commit()

    rejected(api.submit(gpus=1, walltime="04:00"), "QUOTA_EXCEEDED")
    assert job_count(db) == 0


def test_metrics_expose_the_five_signals(api, seed, labs):  # A-76, NFR-GPU-051
    seed.node(labs["NLP"], units=4)
    api.user_id = seed.member(labs["NLP"])
    seed.commit()
    api.submit(gpus=1, request="one")                                        # placed: 3 units left free
    api.submit(gpus=8, request="never")                                      # rejected: never queued

    response = api.client.get("/metrics")

    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    values = dict(line.rsplit(" ", 1) for line in response.text.splitlines() if not line.startswith("#"))
    assert values["prayog_queue_depth"] == "0"
    assert values["prayog_free_units"] == "3"
    assert values["prayog_pass_duration_seconds_count"] == "1"
    assert float(values["prayog_pass_duration_seconds_sum"]) >= 0
    assert values["prayog_claim_failures_total"] == "0"
    assert values["prayog_dispatch_timeouts_total"] == "0"


def test_free_units_exclude_units_in_maintenance_and_unavailable_nodes(api, db, seed, labs):  # NFR-GPU-051
    from logic.gpu.scheduler.claim import utc_now

    node = seed.node(labs["NLP"], units=4)
    seed.node(labs["NLP"], units=4, state=NodeState.DRAINING)                # none of its units count
    now = utc_now()                                                           # /metrics reads the real clock
    db.add(orm.MaintenanceWindow(unit_id=seed.unit_ids(node)[0], reason="test",
                                 starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1)))
    seed.commit()

    values = dict(line.rsplit(" ", 1) for line in api.client.get("/metrics").text.splitlines()
                  if not line.startswith("#"))

    assert values["prayog_free_units"] == "3"
