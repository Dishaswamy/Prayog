"""The fakes must honour port semantics, or tests written against them mislead."""

from datetime import timedelta

from factories import T0, run_view
from prayog_steward.fakes import FakeLedgerHistory, FakeRunReader, FakeSubmissionPort
from prayog_steward.ports import JobSpec, LedgerEntry, SubmissionRequest


def test_fr_gpu_148_earlier_runs_are_same_project_only_and_newest_first() -> None:
    reader = FakeRunReader()
    for i, project in enumerate(["p1", "p2", "p1", "p1"]):
        reader.add(run_view(f"r{i}", project_id=project, ended_at=T0 + timedelta(hours=i)))
    earlier = reader.earlier_runs("p1", before=T0 + timedelta(hours=3), limit=10)
    assert [r.run_id for r in earlier] == ["r2", "r0"]


def test_ledger_history_never_returns_another_users_entries() -> None:
    ledger = FakeLedgerHistory(
        [
            LedgerEntry("e1", "alice", "p1", "metrics", "{}", T0),
            LedgerEntry("e2", "bob", "p1", "metrics", "{}", T0),
        ]
    )
    assert [e.entry_id for e in ledger.for_user("alice", None, 10)] == ["e1"]


def test_requested_gpu_hours_is_gpus_times_walltime() -> None:
    spec = JobSpec("python t.py", (), gpu_count=2, walltime_minutes=90, image_digest="d")
    assert spec.requested_gpu_hours == 3.0


def test_submission_port_can_simulate_rejection() -> None:
    port = FakeSubmissionPort(reject_reason="quota exceeded")
    spec = JobSpec("python t.py", (), gpu_count=1, walltime_minutes=10, image_digest="d")
    receipt = port.submit(
        SubmissionRequest("p1", spec, parent_run_id="r1", approving_user="u1", idempotency_key="k")
    )
    assert not receipt.accepted
    assert receipt.reason == "quota exceeded"
    assert port.submitted == []
