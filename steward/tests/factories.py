"""Builders for test inputs. Keep them small and explicit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from prayog_steward.ports import JobSpec, RunEnd, RunView
from steward_contracts import (
    Context,
    EvidenceRow,
    InvokeOutcome,
    InvokeRequest,
    InvokeResponse,
    Record,
    RecordKind,
    S1Input,
    S2Input,
    S3Input,
    S4Input,
    SearchStatus,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def s1_request(*extra: Record) -> InvokeRequest:
    records = [
        Record(record_id="run:r1", kind=RecordKind.RUN, body='{"exit_code": 1}'),
        Record(record_id="log:r1", kind=RecordKind.LOG_EXCERPT, body="CUDA out of memory"),
        Record(record_id="run:r0", kind=RecordKind.EARLIER_RUN, body='{"end": "SUCCEEDED"}'),
        *extra,
    ]
    return InvokeRequest(invocation_id="inv-1", payload=S1Input(run_id="r1", records=records))


def s2_request() -> InvokeRequest:
    rows = [
        EvidenceRow(
            row_id=f"ev{i}",
            node_id="n1",
            unit_id="n1/gpu0",
            owner_lab=f"lab{i}",
            at=T0 + timedelta(minutes=i),
            exit_code=139,
            failure_category="CRASH",
        )
        for i in range(3)
    ]
    return InvokeRequest(invocation_id="inv-2", payload=S2Input(warning_id="w1", rows=rows))


def s3_request(*, with_external: bool = True) -> InvokeRequest:
    records = [
        Record(record_id="err:a1", kind=RecordKind.LOG_EXCERPT, body="ModuleNotFoundError: x"),
        Record(record_id="led:1", kind=RecordKind.LEDGER, body="torch==2.3.1 in snapshot"),
    ]
    if with_external:
        records.append(
            Record(
                record_id="web:1",
                kind=RecordKind.EXTERNAL,
                body="x was renamed in 2.4",
                source_url="https://example.org/x",
            )
        )
    return InvokeRequest(
        invocation_id="inv-3",
        payload=S3Input(
            attempt_id="a1",
            question="why did it fail?",
            records=records,
            history=[],
            search_status=SearchStatus.OK,
        ),
    )


def s4_request() -> InvokeRequest:
    records = [Record(record_id="met:a2", kind=RecordKind.METRICS, body='{"val_loss": 0.41}')]
    return InvokeRequest(invocation_id="inv-4", payload=S4Input(attempt_id="a2", records=records))


REQUESTS = {
    Context.S1: s1_request,
    Context.S2: s2_request,
    Context.S3: s3_request,
    Context.S4: s4_request,
}


def ok_response(request: InvokeRequest, result: dict[str, Any]) -> InvokeResponse:
    return InvokeResponse(
        invocation_id=request.invocation_id,
        context=request.context,
        outcome=InvokeOutcome.OK,
        result=result,
        agent_id="test",
        agent_version="0",
    )


def run_view(
    run_id: str,
    *,
    project_id: str = "p1",
    owner_id: str = "u1",
    end: RunEnd = RunEnd.FAILED,
    ended_at: datetime = T0,
) -> RunView:
    return RunView(
        run_id=run_id,
        job_id=f"job-{run_id}",
        project_id=project_id,
        owner_id=owner_id,
        owner_lab="vision",
        node_id="n1",
        unit_ids=("n1/gpu0",),
        end=end,
        started_at=ended_at - timedelta(hours=1),
        ended_at=ended_at,
        job_spec=JobSpec(
            command_args="python train.py",
            env_pins=("torch==2.3.1",),
            gpu_count=2,
            walltime_minutes=90,
            image_digest="sha256:abc",
        ),
        exit_code=1 if end is RunEnd.FAILED else 0,
    )
