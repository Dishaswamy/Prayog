"""
The Steward inside the Prayog backend, end to end: real accounts and JWTs, real access
approvals, /jobs submissions placed by real scheduling passes, runs started and ended
through the node agent's own API (acknowledge, then a report carrying the log tail),
node failures found by the reaper, and the Steward's API on the same app.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

import models as orm
from database import SessionLocal, engine
from logic.scheduler.claim import utc_now
from logic.scheduler.domain import JobState, NodeState
from logic.scheduler.reaper import reap

OOM_LOG = """\
Traceback (most recent call last):
  File "/home/alice/vit/train.py", line 141, in <module>
    loss = model(images.cuda(), labels.cuda())
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 3.00 GiB
WANDB_API_KEY=0a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3
"""
REBUILD_LOG = """\
Traceback (most recent call last):
  File "/home/alice/vit/train.py", line 3, in <module>
    import timm.layers.fancy_attention
ModuleNotFoundError: No module named 'timm.layers.fancy_attention'
"""


# ── helpers ─────────────────────────────────────────────────────────────

def login(client: Any, who: str, password: str) -> dict[str, str]:
    r = client.post("/auth/login", json={"email": f"{who}@iitk.ac.in", "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def submit(client: Any, headers: dict[str, str], cid: str, gpus: int = 1,
           snapshot: str | None = None, project: str | None = None) -> int:
    body = {"gpus": gpus, "walltime": "00:30", "client_request_id": cid,
            "snapshot_hash": snapshot, "project_ref": project}
    r = client.post("/jobs", json=body, headers=headers)
    assert r.status_code in (200, 201), r.text
    return int(r.json()["request_id"])


def job_state(job_id: int) -> str:
    with SessionLocal() as s:
        return s.get(orm.Job, job_id).state


def start_run(client: Any, world: dict, job_id: int) -> tuple[int, str]:
    """What the node agent does first: acknowledge the placed job, which starts its run."""
    with SessionLocal() as s:
        allocation_id, node = s.execute(
            select(orm.Allocation.id, orm.Node.name)
            .join(orm.Node, orm.Node.id == orm.Allocation.node_id)
            .where(orm.Allocation.job_id == job_id, orm.Allocation.released_at.is_(None))
        ).one()
    r = client.post(f"/agent/allocations/{allocation_id}/acknowledge", headers=world["agents"][node].headers)
    assert r.status_code == 200 and r.json()["started"], r.text
    return allocation_id, node


def end_run(client: Any, world: dict, job_id: int, exit_code: int = 1, log: str | None = None) -> int:
    """The agent starts the placed job, then reports its exit with the end of its output.
    Returns the run id."""
    allocation_id, node = start_run(client, world, job_id)
    agent = world["agents"][node]
    agent.seq += 1
    r = client.post(f"/agent/allocations/{allocation_id}/report", headers=agent.headers,
                    json={"seq": agent.seq, "outcome": "EXITED", "exit_code": exit_code, "log_tail": log})
    assert r.status_code == 200 and r.json()["applied"], r.text
    with SessionLocal() as s:
        return s.scalar(select(orm.Run.id).where(orm.Run.allocation_id == allocation_id))


def lose_node(name: str) -> None:
    """The node stops heartbeating; the reaper (the scheduler tick) marks it DOWN and
    releases its runs as NODE_FAILURE."""
    with SessionLocal() as s:
        s.execute(update(orm.Node).where(orm.Node.name == name)
                  .values(last_heartbeat_at=utc_now() - timedelta(minutes=10)))
        s.commit()
        reap(s)


def steward() -> Any:
    from integration.steward_bridge import get_steward

    return get_steward()


def count(sql: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text(sql)).scalar_one())


@pytest.fixture(autouse=True)
def _reasoning() -> Iterator[None]:
    """Every test starts with the rule-based adapter (no API key needed)."""
    from steward_runtime.heuristic import HeuristicReasoning

    s = steward()
    s.coordinator.reasoning = s.s3.reasoning = s.s4.reasoning = HeuristicReasoning()
    yield


# ── the platform is unaffected ──────────────────────────────────────────

def test_platform_endpoints_still_work(client: Any, world: dict) -> None:
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}
    alice = login(client, "alice", world["password"])
    assert client.get("/auth/me", headers=alice).json()["role"] == "STUDENT"
    job = submit(client, alice, "plain-1")
    assert job_state(job) == JobState.SCHEDULED          # placed by the triggered pass


def test_steward_routes_use_the_platform_jwt(client: Any, world: dict) -> None:
    assert client.get("/steward/proposals").status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.get("/steward/proposals", headers=bad).status_code == 401
    alice = login(client, "alice", world["password"])
    assert client.get("/steward/proposals", headers=alice).json() == []
    assert client.get("/steward/admin/warnings", headers=alice).status_code == 404
    admin = login(client, "admin", world["password"])
    assert client.get("/steward/admin/warnings", headers=admin).json() == []


def test_a_token_the_platform_refuses_is_refused_by_the_steward(
    client: Any, world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token validity is the platform's get_current_user, so a revocation rule added
    there (e.g. on password reset) also closes the Steward's routes."""
    from fastapi import HTTPException

    import integration.steward_bridge as bridge

    alice = login(client, "alice", world["password"])
    assert client.get("/steward/proposals", headers=alice).status_code == 200

    def revoked(*args: Any, **kwargs: Any) -> Any:
        raise HTTPException(status_code=401, detail="Token revoked")

    monkeypatch.setattr(bridge, "get_current_user", revoked)
    assert client.get("/steward/proposals", headers=alice).status_code == 401


# ── S1: the run-seal hook, diagnosis, visibility ────────────────────────

def test_a_failed_run_is_sealed_for_the_steward_in_the_release(client: Any, world: dict) -> None:
    alice = login(client, "alice", world["password"])
    job = submit(client, alice, "oom-1")
    run = end_run(client, world, job, 1, OOM_LOG)

    assert job_state(job) == JobState.FAILED
    assert count("SELECT count(*) FROM usage_entries") == 1          # platform accounting unchanged
    assert count("SELECT count(*) FROM runs WHERE end_state = 'FAILED' AND exit_code = 1") == 1
    assert count(f"SELECT count(*) FROM run_logs WHERE run_id = {run}") == 1    # from the report
    assert count(f"SELECT count(*) FROM steward.failure_events WHERE run_id = '{run}'") == 1
    assert count("SELECT count(*) FROM steward.fault_evidence WHERE unit_id = 'n1/gpu0'") == 1
    assert count("SELECT count(*) FROM steward.steward_tasks WHERE context = 'S1'") == 1


def test_s1_diagnosis_cites_the_log_and_respects_visibility(client: Any, world: dict) -> None:
    alice = login(client, "alice", world["password"])
    job = submit(client, alice, "oom-2")
    run = end_run(client, world, job, 1, OOM_LOG)
    assert steward().coordinator.drain() == 1

    diag = client.get(f"/steward/runs/{run}/diagnoses", headers=alice).json()
    assert diag[0]["outcome"] == "ok" and diag[0]["classification"] == "oom"
    assert diag[0]["claims"][0]["cites"][0]["kind"] == "log_excerpt"
    assert "0a1b2c3d4e5f" not in str(diag)                           # sanitised (ADR-S03)

    prof = login(client, "prof", world["password"])
    theirs = client.get(f"/steward/runs/{run}/diagnoses", headers=prof).json()
    assert theirs[0]["claims"] == diag[0]["claims"] and theirs[0]["hardware_verdict"] is None
    for other in ("bob", "admin"):                                    # NFR-AUTH-18
        h = login(client, other, world["password"])
        assert client.get(f"/steward/runs/{run}/diagnoses", headers=h).status_code == 404


def _propose(run: int, *changes: tuple[str, str]) -> None:
    """Make the next S1 diagnosis propose these job changes, citing the run's log."""
    from steward_contracts import Context
    from steward_runtime.stub import StubReasoning

    cite = [f"log:{run}:0"]
    steward().coordinator.reasoning = StubReasoning(fixed={Context.S1: {
        "classification": "oom",
        "claims": [{"text": "Out of GPU memory.", "cites": cite}],
        "matching_earlier_failures": [],
        "hardware_finding": {"verdict": "unlikely_hardware", "indicators": []},
        "proposal": {"changes": [
            {"field": f, "new_value": v, "reason": {"text": "Suggested fix.", "cites": cite}}
            for f, v in changes
        ]},
    }})


def test_approving_a_proposal_submits_a_real_job(client: Any, world: dict) -> None:
    alice = login(client, "alice", world["password"])
    job = submit(client, alice, "oom-3")
    run = end_run(client, world, job, 1, OOM_LOG)
    _propose(run, ("gpu_count", "2"), ("batch_size", "64"))  # batch_size: no such job field
    steward().coordinator.drain()

    (proposal,) = client.get("/steward/proposals?state=PROPOSED", headers=alice).json()
    assert [c["field"] for c in proposal["changes"]] == ["gpu_count"]  # batch_size dropped
    assert proposal["proposed_spec"]["gpu_count"] == 2
    assert float(proposal["requested_gpu_hours"]) == 1.0                # 2 GPUs x 30 min

    bob = login(client, "bob", world["password"])
    assert client.post(f"/steward/proposals/{proposal['proposal_id']}/approve",
                       headers=bob).status_code == 404
    decision = client.post(f"/steward/proposals/{proposal['proposal_id']}/approve", headers=alice)
    assert decision.status_code == 200 and decision.json()["state"] == "APPROVED", decision.text
    new_job = int(decision.json()["submitted_job_id"])
    with SessionLocal() as s:
        j = s.get(orm.Job, new_job)
        assert (j.user_id, j.gpus, j.client_request_id) == (
            s.get(orm.Job, job).user_id, 2, f"steward:{proposal['proposal_id']}")
    assert client.post(f"/steward/proposals/{proposal['proposal_id']}/approve",
                       headers=alice).status_code == 409
    assert count("SELECT count(*) FROM audit_events WHERE operation = "
                 "'steward.proposal.approve' AND outcome = 'ok'") == 1


def test_approval_is_refused_after_access_expires(client: Any, world: dict) -> None:
    alice = login(client, "alice", world["password"])
    job = submit(client, alice, "oom-4")
    run = end_run(client, world, job, 1, OOM_LOG)
    _propose(run, ("walltime_minutes", "60"))
    steward().coordinator.drain()
    (proposal,) = client.get("/steward/proposals", headers=alice).json()
    with SessionLocal() as s:                                          # ID-03: access ends
        s.execute(update(orm.Student).values(end_date=orm.Student.start_date))
        s.commit()
    r = client.post(f"/steward/proposals/{proposal['proposal_id']}/approve", headers=alice)
    assert r.status_code == 403


def test_a_broken_steward_never_blocks_a_release(client: Any, world: dict,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    import integration.steward_bridge as bridge

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("steward is down")

    monkeypatch.setattr(bridge, "on_run_sealed", boom)
    alice = login(client, "alice", world["password"])
    job = submit(client, alice, "oom-5")
    end_run(client, world, job, 1, OOM_LOG)
    assert job_state(job) == JobState.FAILED                           # NFR-GPU-055
    assert count("SELECT count(*) FROM usage_entries") == 1
    assert count("SELECT count(*) FROM steward.failure_events") == 0


def test_completions_seal_nothing_and_node_failures_are_evidence_only(client: Any,
                                                                      world: dict) -> None:
    alice = login(client, "alice", world["password"])
    ok = submit(client, alice, "ok-1")
    end_run(client, world, ok, exit_code=0)
    lost = submit(client, alice, "lost-1")
    _, node = start_run(client, world, lost)
    lose_node(node)
    assert job_state(lost) == JobState.QUEUED                          # requeued
    assert count("SELECT count(*) FROM runs WHERE end_state = 'INTERRUPTED'") == 1
    assert count("SELECT count(*) FROM steward.steward_tasks") == 0     # FR-GPU-147
    assert count("SELECT count(*) FROM steward.fault_evidence") == 1


def test_a_job_cancelled_before_it_starts_has_no_run_to_seal(client: Any, world: dict) -> None:
    alice = login(client, "alice", world["password"])
    job = submit(client, alice, "early-cancel")
    r = client.post(f"/jobs/{job}/cancel", headers=alice)
    assert r.status_code == 200 and r.json()["state"] == JobState.CANCELLED, r.text
    assert count("SELECT count(*) FROM runs") == 0
    assert count("SELECT count(*) FROM steward.failure_events") == 0


# ── S2: hardware warnings ───────────────────────────────────────────────

def test_two_users_failing_on_one_gpu_raise_a_warning_for_the_admin(client: Any,
                                                                    world: dict) -> None:
    alice = login(client, "alice", world["password"])
    bob = login(client, "bob", world["password"])
    with SessionLocal() as s:                       # bob's own node is down, so he bursts to n1
        s.execute(update(orm.Node).where(orm.Node.name == "n2").values(state=NodeState.DOWN))
        s.commit()
    end_run(client, world, submit(client, alice, "hw-a"), 1, "Xid 79: GPU has fallen off the bus")
    end_run(client, world, submit(client, bob, "hw-b"), 1, "Xid 79: GPU has fallen off the bus")
    steward().coordinator.drain()

    admin = login(client, "admin", world["password"])
    (warning,) = client.get("/steward/admin/warnings?state=OPEN", headers=admin).json()
    assert warning["unit_signature"] == "n1/gpu0" and warning["distinct_labs"] == 2
    detail = client.get(f"/steward/admin/warnings/{warning['warning_id']}", headers=admin).json()
    assert {e["owner_lab"] for e in detail["evidence"]} == {"vision", "nlp"}
    assert len(detail["allocation_history"]) == 2
    assert detail["steward_finding"]["same_fault"] is True             # advisory S2
    r = client.post(f"/steward/admin/warnings/{warning['warning_id']}/close", headers=admin,
                    json={"resolution": "RESOLVED", "reason": "froze n1/gpu0"})
    assert r.json()["state"] == "RESOLVED"


# ── S3 and S4 through the interim rebuild endpoints ─────────────────────

def _rebuild(client: Any, headers: dict[str, str], cid: str, snapshot: str = "snap-1") -> dict:
    r = client.post("/rebuilds", headers=headers, json={
        "snapshot_hash": snapshot, "gpus": 1, "walltime": "00:30", "client_request_id": cid})
    return {"status": r.status_code, **r.json()}


def test_rebuild_rejects_snapshots_the_user_never_ran(client: Any, world: dict) -> None:
    alice = login(client, "alice", world["password"])
    bob = login(client, "bob", world["password"])
    end_run(client, world, submit(client, alice, "orig-1", snapshot="snap-1", project="vit"), exit_code=0)
    rejected = _rebuild(client, bob, "rb-x")                           # not bob's snapshot
    assert rejected["status"] == 404 and rejected["code"] == "REBUILD_REJECTED"
    assert count("SELECT count(*) FROM rebuild_attempts") == 0


def test_failed_rebuild_opens_an_s3_session_and_success_allows_s4(client: Any,
                                                                   world: dict) -> None:
    alice = login(client, "alice", world["password"])
    end_run(client, world, submit(client, alice, "orig-2", snapshot="snap-1", project="vit"), exit_code=0)

    failed = _rebuild(client, alice, "rb-1")
    assert failed["status"] == 201 and failed["state"] == "REBUILD_INITIATED"
    end_run(client, world, failed["job_id"], 1, REBUILD_LOG)
    attempt = client.get(f"/rebuilds/{failed['attempt_id']}", headers=alice).json()
    assert attempt["state"] == "REBUILD_FAILED"

    link = client.post(f"/steward/attempts/{failed['attempt_id']}/link", headers=alice)
    assert link.status_code == 200, link.text
    path = link.json()["url"].split("://", 1)[1].split("/", 1)[1]
    assert client.get("/" + path).status_code == 200                  # the page; no side effects
    bob = login(client, "bob", world["password"])
    assert client.post(f"/{path}/open", headers=bob).status_code == 403   # A11
    opened = client.post(f"/{path}/open", headers=alice)
    assert opened.status_code == 200 and opened.json()["attempt_id"] == str(failed["attempt_id"])
    assert client.post(f"/{path}/open", headers=alice).status_code == 403  # A10 single use

    answer = client.post("/steward/s3/messages", headers=alice,
                         json={"question": "Why did my rebuild fail?"}).json()
    assert answer["outcome"] == "ok" and answer["evidence_found"] is True
    assert any(c["provenance"] == "internal" for c in answer["claims"])
    assert "/home/alice" not in str(answer)

    good = _rebuild(client, alice, "rb-2")
    end_run(client, world, good["job_id"], exit_code=0)
    advice = client.post(f"/steward/attempts/{good['attempt_id']}/optimize", headers=alice)
    assert advice.status_code == 200 and advice.json()["outcome"] == "ok"
    assert client.post(f"/steward/attempts/{failed['attempt_id']}/optimize",
                       headers=alice).status_code == 403                # A06


# ── the audit log ───────────────────────────────────────────────────────

def test_steward_activity_lands_in_the_append_only_platform_audit_log(client: Any,
                                                                      world: dict) -> None:
    alice = login(client, "alice", world["password"])
    job = submit(client, alice, "audit-1")
    end_run(client, world, job, 1, OOM_LOG)
    steward().coordinator.drain()
    assert count("SELECT count(*) FROM audit_events WHERE operation = "
                 "'steward.diagnosis.recorded' AND actor_kind = 'service'") == 1
    with engine.begin() as c, pytest.raises(DBAPIError):
        c.execute(text("UPDATE audit_events SET outcome = 'tampered'"))


def test_the_steward_role_cannot_bypass_row_level_security() -> None:
    from integration.steward_bridge import get_steward

    with get_steward().engine.connect() as c:
        row = c.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
    assert tuple(row) == (False, False)


def test_processes_that_release_runs_can_register_the_hook_alone() -> None:
    """The scheduler tick (and a future agent gateway) release runs without serving
    HTTP; register_steward_hooks() gives them the same run-seal hook (idempotent)."""
    from integration.steward_bridge import register_steward_hooks, seal_hook
    from logic.scheduler.lifecycle import RELEASE_HOOKS

    assert register_steward_hooks() and register_steward_hooks()
    assert RELEASE_HOOKS.count(seal_hook) == 1
