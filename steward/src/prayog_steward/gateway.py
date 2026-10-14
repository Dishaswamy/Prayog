"""Gateway: the only path from Prayog data to the model (ADR-S01, SADD §8.2).

Each invocation gets a Scope derived from its subject: the failed run (S1), the warning
(S2) or the rebuild attempt (S3/S4). The scope is fixed by the server, never by the model.
Everything read is checked against the scope again, even when a port should already have
filtered it (defence in depth). Anything outside is dropped and the refusal audited
(FR-GPU-148, 151). Every record body passes through the sanitiser (ADR-S03).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, select

from prayog_steward.audit import Auditor
from prayog_steward.config import StewardSettings
from prayog_steward.coordinator.proposals import changeable_fields
from prayog_steward.db.models import diagnoses, fault_evidence, warning_evidence
from prayog_steward.ports import JobSpec, LogStore, RunReader, RunView
from prayog_steward.rag.sanitiser import Sanitiser, excerpt
from steward_contracts import (
    Context,
    EvidenceRow,
    InvokeRequest,
    Record,
    RecordKind,
    S1Input,
    S2Input,
)


@dataclass(frozen=True)
class Scope:
    invocation_id: str
    context: Context
    subject_id: str
    owner_id: str | None
    project_id: str | None
    issued_at: datetime
    expires_at: datetime


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def job_spec_dict(spec: JobSpec) -> dict[str, Any]:
    return {
        "command_args": spec.command_args,
        "env_pins": list(spec.env_pins),
        "gpu_count": spec.gpu_count,
        "walltime_minutes": spec.walltime_minutes,
        "batch_size": spec.batch_size,
        "precision": spec.precision,
        "image_digest": spec.image_digest,
    }


class Gateway:
    def __init__(
        self,
        *,
        runs: RunReader,
        logs: LogStore,
        sanitiser: Sanitiser,
        settings: StewardSettings,
        auditor: Auditor,
    ) -> None:
        self.runs = runs
        self.logs = logs
        self.sanitiser = sanitiser
        self.settings = settings
        self.auditor = auditor

    # ------------------------------------------------------------ helpers

    def record(self, record_id: str, kind: RecordKind, body: str | dict[str, Any]) -> Record:
        text = body if isinstance(body, str) else json.dumps(body, sort_keys=True, default=str)
        return Record(record_id=record_id, kind=kind, body=self.sanitiser.sanitise(text).text)

    def scope(
        self,
        context: Context,
        subject_id: str,
        owner_id: str | None,
        project_id: str | None,
        now: datetime,
    ) -> Scope:
        return Scope(
            invocation_id=str(uuid.uuid4()),
            context=context,
            subject_id=subject_id,
            owner_id=owner_id,
            project_id=project_id,
            issued_at=now,
            expires_at=now + timedelta(minutes=10),
        )

    def _refuse(self, scope: Scope, what: str) -> None:
        self.auditor.by_steward(
            "steward.scope.refused",
            scope.invocation_id,
            scope.subject_id,
            outcome="refused",
            reason=what,
        )

    # ------------------------------------------------------------ S1

    def run_record(self, run: RunView) -> Record:
        minutes = round((run.ended_at - run.started_at).total_seconds() / 60, 1)
        return self.record(
            f"run:{run.run_id}",
            RecordKind.RUN,
            {
                "run_id": run.run_id,
                "job_id": run.job_id,
                "end": run.end.value,
                "exit_code": run.exit_code,
                "failure_category": run.failure_category,
                "node_id": run.node_id,
                "unit_ids": list(run.unit_ids),
                "started_at": _iso(run.started_at),
                "ended_at": _iso(run.ended_at),
                "duration_minutes": minutes,
                "job": job_spec_dict(run.job_spec),
                "changeable_fields": [f.value for f in changeable_fields(self.settings)],
            },
        )

    def log_records(self, run: RunView) -> list[Record]:
        out: list[Record] = []
        budget = self.settings.log_excerpt_max_chars
        for i, ref in enumerate(run.log_refs):
            raw = self.logs.read_log(ref)
            if not raw:
                continue
            clean = self.sanitiser.sanitise(raw).text  # sanitise first, then cut
            out.append(
                Record(
                    record_id=f"log:{run.run_id}:{i}",
                    kind=RecordKind.LOG_EXCERPT,
                    body=excerpt(clean, budget),
                )
            )
        return out

    def s1_request(
        self, conn: Connection, run: RunView, now: datetime
    ) -> tuple[Scope, InvokeRequest]:
        scope = self.scope(Context.S1, run.run_id, run.owner_id, run.project_id, now)
        records = [self.run_record(run)]
        telemetry = self.runs.telemetry(run.run_id)
        if telemetry is not None and telemetry.run_id == run.run_id:
            records.append(
                self.record(f"telemetry:{run.run_id}", RecordKind.TELEMETRY, telemetry.summary)
            )
        records.extend(self.log_records(run))
        records.extend(self._earlier_runs(conn, scope, run))
        request = InvokeRequest(
            invocation_id=scope.invocation_id,
            payload=S1Input(run_id=run.run_id, records=records),
        )
        return scope, request

    def _earlier_runs(self, conn: Connection, scope: Scope, run: RunView) -> list[Record]:
        earlier: Sequence[RunView] = self.runs.earlier_runs(
            run.project_id, before=run.ended_at, limit=self.settings.earlier_runs_limit
        )
        kept: list[RunView] = []
        for other in earlier:
            if other.project_id != run.project_id or other.run_id == run.run_id:
                self._refuse(scope, "earlier run outside the project")  # FR-GPU-148
                continue
            kept.append(other)
        previous = self._latest_classifications(conn, [r.run_id for r in kept])
        return [
            self.record(
                f"earlier:{r.run_id}",
                RecordKind.EARLIER_RUN,
                {
                    "run_id": r.run_id,
                    "end": r.end.value,
                    "ended_at": _iso(r.ended_at),
                    "exit_code": r.exit_code,
                    "failure_category": r.failure_category,
                    "job": job_spec_dict(r.job_spec),
                    "previous_diagnosis": previous.get(r.run_id),
                },
            )
            for r in kept
        ]

    @staticmethod
    def _latest_classifications(conn: Connection, run_ids: list[str]) -> dict[str, str]:
        if not run_ids:
            return {}
        rows = conn.execute(
            select(diagnoses.c.run_id, diagnoses.c.classification)
            .where(
                diagnoses.c.run_id.in_(run_ids),
                diagnoses.c.outcome == "ok",
                diagnoses.c.classification.is_not(None),
            )
            .order_by(diagnoses.c.created_at)
        ).all()
        return {r.run_id: r.classification for r in rows}

    # ------------------------------------------------------------ S2

    def s2_request(
        self, conn: Connection, warning_id: uuid.UUID, now: datetime
    ) -> tuple[Scope, InvokeRequest]:
        """Metadata-only bundle of the warning's cited evidence, newest first, at most
        ``max_evidence_bundle_rows`` rows (FR-GPU-165, NFR-GPU-056)."""
        scope = self.scope(Context.S2, str(warning_id), None, None, now)
        rows = conn.execute(
            select(fault_evidence)
            .join(warning_evidence, warning_evidence.c.evidence_id == fault_evidence.c.evidence_id)
            .where(warning_evidence.c.warning_id == warning_id)
            .order_by(fault_evidence.c.at.desc())
            .limit(self.settings.max_evidence_bundle_rows)
        ).all()
        bundle = [
            EvidenceRow(
                row_id=f"ev:{r.evidence_id}",
                node_id=r.node_id,
                unit_id=r.unit_id,
                owner_lab=r.owner_lab,
                at=r.at,
                exit_code=r.exit_code,
                failure_category=r.failure_category,
                failure_signature=r.failure_signature,
            )
            for r in rows
        ]
        request = InvokeRequest(
            invocation_id=scope.invocation_id,
            payload=S2Input(warning_id=str(warning_id), rows=bundle),
        )
        return scope, request
