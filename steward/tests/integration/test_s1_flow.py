"""S1 end to end: seal -> task -> scoped input -> model -> validation -> persistence ->
owner approval (SD-15, UC-GPU-20)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from harness import Harness, user
from sqlalchemy import Engine, func, select, text

from prayog_steward.coordinator import tasks
from prayog_steward.db.models import (
    diagnoses,
    failure_events,
    fault_evidence,
    hardware_findings,
    proposals,
    steward_tasks,
)
from prayog_steward.owner import Conflict, Forbidden, NotFound
from prayog_steward.ports import RunEnd
from steward_contracts import Context, InvokeRequest, S1Input
from steward_runtime import StubBehaviour, StubReasoning

LOG = """\
Traceback (most recent call last):
  File "/home/alice/proj/train.py", line 88, in <module>
    out = model(batch, api_key="sk-ant-api03-SECRETSECRETSECRET1234")
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 4.00 GiB
"""


def s1_fixed(**overrides: Any) -> dict[Any, dict[str, Any]]:
    result: dict[str, Any] = {
        "classification": "oom",
        "claims": [
            {"text": "The run ran out of GPU memory at batch size 64.", "cites": ["log:r1:0"]},
            {"text": "Invented claim with no evidence.", "cites": []},
        ],
        "matching_earlier_failures": [],
        "hardware_finding": {"verdict": "unlikely_hardware", "indicators": []},
        "proposal": {
            "changes": [
                {
                    "field": "batch_size",
                    "new_value": "32",
                    "reason": {"text": "Halve the batch to fit memory.", "cites": ["log:r1:0"]},
                },
                {
                    "field": "gpu_count",
                    "new_value": "64",
                    "reason": {"text": "Way more GPUs.", "cites": ["log:r1:0"]},
                },
            ]
        },
    }
    return {Context.S1: {**result, **overrides}}


@pytest.fixture
def h(app_engine: Engine) -> Harness:
    return Harness(app_engine, reasoning=StubReasoning(fixed=s1_fixed()))


def _count(engine: Engine, table: Any) -> int:
    with engine.connect() as c:
        return int(c.execute(select(func.count()).select_from(table)).scalar_one())


def test_fr_gpu_146_failed_run_records_event_evidence_and_task(h: Harness) -> None:
    out = h.seal(h.failed_run("r1", units=("n1/gpu0", "n1/gpu1"), batch_size=64))
    assert out.diagnosis_enqueued and out.evidence_rows == 2
    assert _count(h.engine, failure_events) == 1
    assert _count(h.engine, fault_evidence) == 2
    assert _count(h.engine, steward_tasks) == 1


def test_sealing_twice_is_idempotent(h: Harness) -> None:
    run = h.failed_run("r1")
    h.seal(run)
    again = h.seal(run)
    assert not again.diagnosis_enqueued and again.evidence_rows == 0
    assert _count(h.engine, steward_tasks) == 1


@pytest.mark.parametrize(
    "end", [RunEnd.CANCELLED, RunEnd.WALLTIME_EXCEEDED, RunEnd.SUCCEEDED, RunEnd.INTERRUPTED]
)
def test_fr_gpu_147_non_failures_trigger_nothing(h: Harness, end: RunEnd) -> None:
    from dataclasses import replace

    run = replace(h.failed_run("r1"), end=end, interrupt_reason="PREEMPTED_BY_ADMIN")
    assert h.seal(run).evidence_rows == 0
    assert _count(h.engine, steward_tasks) == 0


def test_fr_gpu_147_node_failure_is_evidence_without_diagnosis(h: Harness) -> None:
    from dataclasses import replace

    run = replace(h.failed_run("r1"), end=RunEnd.INTERRUPTED, interrupt_reason="NODE_FAILURE")
    out = h.seal(run)
    assert out.evidence_rows == 1 and not out.diagnosis_enqueued
    assert _count(h.engine, failure_events) == 0


def test_nfr_gpu_055_sealing_never_calls_the_model(h: Harness) -> None:
    h.seal(h.failed_run("r1", log=LOG))
    assert h.reasoning.calls == []


def test_s1_end_to_end_cited_diagnosis_finding_and_bounded_proposal(h: Harness) -> None:
    h.seal(h.failed_run("r1", log=LOG, batch_size=64))
    assert h.coordinator.drain() == 1

    with h.engine.connect() as c:
        d = c.execute(select(diagnoses)).one()
        f = c.execute(select(hardware_findings)).one()
        p = c.execute(select(proposals)).one()
        task_state = c.execute(select(steward_tasks.c.state)).scalar_one()
    assert task_state == "DONE"
    assert (d.outcome, d.classification, d.stripped_count) == ("ok", "oom", 1)  # FR-GPU-161
    assert [c["text"] for c in d.claims] == ["The run ran out of GPU memory at batch size 64."]
    assert d.claims[0]["cites"] == [{"record_id": "log:r1:0", "kind": "log_excerpt"}]
    assert d.agent_version == "stub-1"  # FR-GPU-160
    assert f.verdict == "unlikely_hardware"
    # gpu_count=64 is over max_gpu_count, so only the batch-size change survives.
    assert [ch["field"] for ch in p.changes] == ["batch_size"]
    assert p.job_spec["batch_size"] == 32 and p.job_spec["gpu_count"] == 2
    assert p.requested_gpu_hours == Decimal("3.00")  # 2 GPUs x 90 min (FR-GPU-187)
    assert p.state == "PROPOSED"  # FR-GPU-149: nothing queued yet
    assert h.submissions.submitted == []
    assert "steward.diagnosis.recorded" in h.audit.operations()


def test_adr_s03_model_input_is_sanitised_and_scoped(h: Harness) -> None:
    h.seal(h.failed_run("r0", project="p1", end=RunEnd.SUCCEEDED, minutes_ago=60))
    h.runs.add(h.failed_run("other", project="p2"))  # another project's run
    h.seal(h.failed_run("r1", project="p1", log=LOG))
    h.coordinator.drain()

    request: InvokeRequest = h.reasoning.calls[-1]
    assert isinstance(request.payload, S1Input)
    blob = request.model_dump_json()
    assert "SECRETSECRET" not in blob and "/home/alice" not in blob and "api_key=" not in blob
    assert "torch.cuda.OutOfMemoryError" in blob
    ids = {r.record_id for r in request.payload.records}
    assert "earlier:r0" in ids and "earlier:other" not in ids  # FR-GPU-148


def test_fr_gpu_151_write_attempt_is_refused_and_audited(app_engine: Engine) -> None:
    h = Harness(app_engine, reasoning=StubReasoning(StubBehaviour.WRITE_ATTEMPT))
    h.seal(h.failed_run("r1", log=LOG))
    h.coordinator.drain()
    with h.engine.connect() as c:
        assert c.execute(select(diagnoses.c.outcome)).scalar_one() == "malformed"
    assert _count(h.engine, proposals) == 0
    refused = [e for e in h.audit.events if e.operation == "steward.write.refused"]
    assert refused and refused[0].actor == "svc:steward"


def test_nfr_avail_001_unavailable_model_retries_then_records_unavailable(
    app_engine: Engine,
) -> None:
    h = Harness(app_engine, reasoning=StubReasoning(StubBehaviour.UNAVAILABLE))
    h.seal(h.failed_run("r1"))
    for _ in range(h.settings.task_max_attempts):
        assert h.coordinator.run_once()
        assert _count(h.engine, diagnoses) == 0 or _ == h.settings.task_max_attempts - 1
        h.clock.advance(timedelta(hours=2))  # past any backoff
    with h.engine.connect() as c:
        assert c.execute(select(steward_tasks.c.state)).scalar_one() == "GAVE_UP"
        assert c.execute(select(diagnoses.c.outcome)).scalar_one() == "unavailable"
    assert not h.coordinator.run_once()


def test_retry_waits_for_backoff(app_engine: Engine) -> None:
    h = Harness(app_engine, reasoning=StubReasoning(StubBehaviour.UNAVAILABLE))
    h.seal(h.failed_run("r1"))
    assert h.coordinator.run_once()
    assert not h.coordinator.run_once()  # not due yet


def test_crashed_worker_lease_expires_and_task_is_reclaimed(h: Harness) -> None:
    h.seal(h.failed_run("r1"))
    first = tasks.claim(h.engine, h.clock.now(), h.settings.task_lease)
    assert first is not None
    assert tasks.claim(h.engine, h.clock.now(), h.settings.task_lease) is None  # leased
    h.clock.advance(h.settings.task_lease + timedelta(seconds=1))
    again = tasks.claim(h.engine, h.clock.now(), h.settings.task_lease)
    assert again is not None and again.task_id == first.task_id and again.attempts == 2


def _proposal_id(h: Harness) -> Any:
    h.seal(h.failed_run("r1", log=LOG, batch_size=64))
    h.coordinator.drain()
    with h.engine.connect() as c:
        return c.execute(select(proposals.c.proposal_id)).scalar_one()


def test_uc_gpu_20_owner_approves_and_job_goes_through_ordinary_submission(h: Harness) -> None:
    pid = _proposal_id(h)
    view = h.owner.get_proposal(user("alice"), pid)
    assert view.current_spec is not None and view.current_spec["batch_size"] == 64
    assert view.proposed_spec["batch_size"] == 32  # the exact change is shown (FR-GPU-187)

    decision = h.owner.approve(user("alice"), pid)
    assert decision.state == "APPROVED" and decision.submitted_job_id == "job-1"
    sub = h.submissions.submitted[0]
    assert (sub.parent_run_id, sub.approving_user, sub.idempotency_key) == ("r1", "alice", str(pid))
    assert sub.job_spec.batch_size == 32
    with pytest.raises(Conflict):
        h.owner.approve(user("alice"), pid)


def test_only_the_owner_can_see_or_decide_a_proposal(h: Harness) -> None:
    pid = _proposal_id(h)
    for action in (h.owner.get_proposal, h.owner.approve, h.owner.reject):
        with pytest.raises(NotFound):  # NFR-AUTH-17: indistinguishable from absent
            action(user("mallory"), pid)
    assert h.owner.list_proposals(user("mallory")) == []


def test_id_03_approval_is_refused_after_authorization_expiry(h: Harness) -> None:
    pid = _proposal_id(h)
    h.authorizer.inactive_users.add("alice")
    with pytest.raises(Forbidden):
        h.owner.approve(user("alice"), pid)
    assert h.submissions.submitted == []


def test_submission_refusal_keeps_the_proposal_open(h: Harness) -> None:
    pid = _proposal_id(h)
    h.submissions.reject_reason = "lab GPU-hour cap reached"
    decision = h.owner.approve(user("alice"), pid)
    assert decision.state == "PROPOSED" and decision.refusal == "lab GPU-hour cap reached"
    h.submissions.reject_reason = None
    assert h.owner.approve(user("alice"), pid).state == "APPROVED"


def test_fr_gpu_167_rejected_proposal_is_retained(h: Harness) -> None:
    pid = _proposal_id(h)
    assert h.owner.reject(user("alice"), pid).state == "REJECTED"
    assert h.owner.get_proposal(user("alice"), pid).state == "REJECTED"
    assert h.submissions.submitted == []
    with h.engine.connect() as c, pytest.raises(Exception):  # noqa: B017
        c.execute(text("DELETE FROM steward.proposals"))


def test_diagnosis_visibility_owner_professor_admin(h: Harness) -> None:
    h.seal(h.failed_run("r1", log=LOG))
    h.coordinator.drain()
    mine = h.owner.diagnoses_for_run(user("alice"), "r1")
    assert mine[0].hardware_verdict == "unlikely_hardware"

    h.authorizer.supervisors["prof"] = {"alice"}
    theirs = h.owner.diagnoses_for_run(user("prof"), "r1")
    assert theirs[0].claims == mine[0].claims and theirs[0].hardware_verdict is None

    with pytest.raises(NotFound):  # NFR-AUTH-18: the admin role grants no content access
        h.owner.diagnoses_for_run(user("root", "platform_admin"), "r1")
