"""S2 and hardware warnings: threshold, attach, cooldown, close, advice, admin view
(SD-16, UC-GPU-21, FR-GPU-152, 158, 162-165)."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from harness import Harness, settings
from sqlalchemy import Engine, func, select

from prayog_steward.db.models import fault_evidence, hardware_warnings, steward_tasks
from prayog_steward.hardware import (
    Evaluation,
    Resolution,
    WarningNotOpen,
    close_warning,
    list_warnings,
    warning_detail,
)
from prayog_steward.hooks import on_run_sealed
from prayog_steward.ports import AllocationRecord
from steward_contracts import Context, S2Input
from steward_runtime import StubReasoning


@pytest.fixture
def h(app_engine: Engine) -> Harness:
    return Harness(app_engine)


def _warnings(engine: Engine) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(select(hardware_warnings).order_by(hardware_warnings.c.opened_at)))


def test_one_users_repeated_failures_never_raise_a_warning(h: Harness) -> None:
    for i in range(5):
        out = h.seal(h.failed_run(f"r{i}", owner="alice"))
        assert out.warnings["n1/gpu0"] is Evaluation.BELOW_THRESHOLD
    assert _warnings(h.engine) == []


def test_fr_gpu_162_two_distinct_users_on_one_unit_raise_open_warning_and_s2_task(
    h: Harness,
) -> None:
    h.seal(h.failed_run("r1", owner="alice"))
    out = h.seal(h.failed_run("r2", owner="bob"))
    assert out.warnings["n1/gpu0"] is Evaluation.RAISED
    (w,) = _warnings(h.engine)
    assert (w.state, w.unit_signature, w.node_id) == ("OPEN", "n1/gpu0", "n1")
    with h.engine.connect() as c:
        contexts = [r.context for r in c.execute(select(steward_tasks.c.context))]
    assert contexts.count(Context.S2.value) == 1


def test_failures_outside_the_window_do_not_count(h: Harness) -> None:
    h.seal(h.failed_run("r1", owner="alice", minutes_ago=0))
    h.clock.advance(h.settings.correlation_window + timedelta(minutes=1))
    out = h.seal(h.failed_run("r2", owner="bob", minutes_ago=0))
    assert out.warnings["n1/gpu0"] is Evaluation.BELOW_THRESHOLD


def test_different_units_are_separate_incidents(h: Harness) -> None:
    h.seal(h.failed_run("r1", owner="alice", units=("n1/gpu0",)))
    out = h.seal(h.failed_run("r2", owner="bob", units=("n1/gpu1",)))
    assert out.warnings["n1/gpu1"] is Evaluation.BELOW_THRESHOLD


def test_fr_gpu_164_further_failures_attach_to_the_open_warning(h: Harness) -> None:
    h.seal(h.failed_run("r1", owner="alice"))
    h.seal(h.failed_run("r2", owner="bob"))
    out = h.seal(h.failed_run("r3", owner="carol"))
    assert out.warnings["n1/gpu0"] is Evaluation.ATTACHED
    (w,) = list_warnings(h.engine)
    assert w.evidence_count == 3 and w.distinct_labs == 3


def test_s2_advice_is_attached_and_cites_only_bundle_rows(h: Harness) -> None:
    h.seal(h.failed_run("r1", owner="alice"))
    h.seal(h.failed_run("r2", owner="bob"))
    h.coordinator.drain()  # runs the S2 task (S1 tasks too, with the default stub)
    s2_calls = [c for c in h.reasoning.calls if isinstance(c.payload, S2Input)]
    assert len(s2_calls) == 1
    bundle = s2_calls[0].payload
    assert isinstance(bundle, S2Input) and len(bundle.rows) == 2
    blob = bundle.model_dump_json()
    assert "owner_id" not in blob and "run_id" not in blob  # labs and units only
    (w,) = list_warnings(h.engine)
    assert w.steward_finding is not None and w.steward_finding["same_fault"] is True
    assert all(
        c["record_id"].startswith("ev:") for r in w.steward_finding["rationale"] for c in r["cites"]
    )


def test_fr_gpu_165_bundle_is_capped(app_engine: Engine) -> None:
    h = Harness(app_engine, settings=settings(max_evidence_bundle_rows=3))
    for i in range(6):
        h.seal(h.failed_run(f"r{i}", owner=f"user{i}"))
    h.coordinator.drain()
    bundles = [c.payload for c in h.reasoning.calls if isinstance(c.payload, S2Input)]
    assert bundles and all(len(b.rows) <= 3 for b in bundles)


def test_fr_gpu_163_close_requires_reason_and_consumes_evidence(h: Harness) -> None:
    h.seal(h.failed_run("r1", owner="alice"))
    h.seal(h.failed_run("r2", owner="bob"))
    wid = _warnings(h.engine)[0].warning_id
    with pytest.raises(ValueError):
        close_warning(
            h.engine,
            wid,
            admin_id="root",
            resolution=Resolution.RESOLVED,
            reason="  ",
            now=h.clock.now(),
        )
    close_warning(
        h.engine,
        wid,
        admin_id="root",
        resolution=Resolution.RESOLVED,
        reason="froze n1/gpu0 for ECC errors",
        now=h.clock.now(),
    )
    with h.engine.connect() as c:
        unconsumed = c.execute(
            select(func.count()).where(fault_evidence.c.consumed_at.is_(None))
        ).scalar_one()
    assert unconsumed == 0
    with pytest.raises(WarningNotOpen):
        close_warning(
            h.engine,
            wid,
            admin_id="root",
            resolution=Resolution.DISMISSED,
            reason="again",
            now=h.clock.now(),
        )
    # Closing consumed the evidence: the next single failure starts a fresh incident.
    out = h.seal(h.failed_run("r3", owner="carol"))
    assert out.warnings["n1/gpu0"] is Evaluation.BELOW_THRESHOLD


def test_dismissal_starts_a_cooldown(h: Harness) -> None:
    h.seal(h.failed_run("r1", owner="alice"))
    h.seal(h.failed_run("r2", owner="bob"))
    wid = _warnings(h.engine)[0].warning_id
    close_warning(
        h.engine,
        wid,
        admin_id="root",
        resolution=Resolution.DISMISSED,
        reason="different library versions, not hardware",
        now=h.clock.now(),
    )
    h.seal(h.failed_run("r3", owner="carol"))
    out = h.seal(h.failed_run("r4", owner="dave"))
    assert out.warnings["n1/gpu0"] is Evaluation.COOLDOWN
    h.clock.advance(h.settings.warning_cooldown + timedelta(minutes=1))
    out = h.seal(h.failed_run("r5", owner="erin", minutes_ago=0))
    out = h.seal(h.failed_run("r6", owner="frank", minutes_ago=0))
    assert out.warnings["n1/gpu0"] is Evaluation.RAISED


def test_fr_gpu_152_a_warning_never_touches_node_state(h: Harness) -> None:
    """The Steward has no node or unit tables and no port that changes them; the only
    effect of a warning is rows in steward.hardware_warnings."""
    import prayog_steward.hardware as hw
    import prayog_steward.ports as ports

    assert not any("freeze" in n or "drain" in n for n in dir(ports))
    assert not any("freeze" in n or "drain" in n for n in dir(hw))


def test_fr_gpu_158_admin_view_is_metadata_only(h: Harness) -> None:
    h.allocations.records.append(
        AllocationRecord("n1/gpu0", "job-x", "lab-zed", h.clock.now() - timedelta(hours=2), None)
    )
    h.seal(h.failed_run("r1", owner="alice"))
    h.seal(h.failed_run("r2", owner="bob"))
    h.coordinator.drain()
    wid = uuid.UUID(list_warnings(h.engine)[0].warning_id)
    view = warning_detail(h.engine, wid, h.allocations, h.settings)
    assert view is not None and len(view.evidence) == 2
    ev = view.evidence[0]
    assert set(ev.__dataclass_fields__) == {
        "evidence_id",
        "run_id",
        "job_id",
        "owner_lab",
        "node_id",
        "unit_id",
        "at",
        "exit_code",
        "failure_category",
        "hardware_verdict",
        "hardware_indicators",
    }  # no owner user, no narrative, no logs, no metrics
    assert ev.hardware_verdict == "inconclusive"  # from each run's S1 hardware finding
    assert [a.job_id for a in view.allocation_history] == ["job-x"]


def test_concurrent_seals_cannot_both_miss_the_threshold(app_engine: Engine) -> None:
    """Two failures from different users committed at the same moment: the per-unit
    advisory lock makes the second see the first, so exactly one warning is raised."""
    h = Harness(app_engine, reasoning=StubReasoning())
    runs = [h.failed_run("ra", owner="alice"), h.failed_run("rb", owner="bob")]
    for r in runs:
        h.runs.add(r)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def seal(run: Any) -> None:
        try:
            with app_engine.begin() as conn:
                barrier.wait()
                on_run_sealed(conn, run, settings=h.settings, clock=h.clock)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=seal, args=(r,)) for r in runs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert len(_warnings(app_engine)) == 1


def test_s2_skips_a_warning_closed_before_the_worker_ran(h: Harness) -> None:
    h.seal(h.failed_run("r1", owner="alice"))
    h.seal(h.failed_run("r2", owner="bob"))
    wid = _warnings(h.engine)[0].warning_id
    close_warning(
        h.engine,
        wid,
        admin_id="root",
        resolution=Resolution.DISMISSED,
        reason="not hardware",
        now=h.clock.now(),
    )
    h.coordinator.drain()
    assert not any(isinstance(c.payload, S2Input) for c in h.reasoning.calls)
    assert _warnings(h.engine)[0].steward_finding is None
