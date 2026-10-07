"""In-memory stand-ins for the other teams' modules (ADR-S07).

Used by tests and by the local demo until the real implementations land. They follow
the port semantics exactly, including owner filtering, so that tests written against
them stay valid when the real modules are wired in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from prayog_steward.ports import (
    AllocationRecord,
    AttemptView,
    AuditEvent,
    LedgerEntry,
    Principal,
    RunView,
    SubmissionReceipt,
    SubmissionRequest,
    TelemetryView,
)


@dataclass
class FakeRunReader:
    runs: dict[str, RunView] = field(default_factory=dict)
    telemetry_by_run: dict[str, TelemetryView] = field(default_factory=dict)

    def add(self, run: RunView, telemetry: TelemetryView | None = None) -> None:
        self.runs[run.run_id] = run
        if telemetry is not None:
            self.telemetry_by_run[run.run_id] = telemetry

    def get_run(self, run_id: str) -> RunView | None:
        return self.runs.get(run_id)

    def earlier_runs(self, project_id: str, before: datetime, limit: int) -> Sequence[RunView]:
        same_project = [
            r for r in self.runs.values() if r.project_id == project_id and r.ended_at < before
        ]
        return sorted(same_project, key=lambda r: r.ended_at, reverse=True)[:limit]

    def telemetry(self, run_id: str) -> TelemetryView | None:
        return self.telemetry_by_run.get(run_id)


@dataclass
class FakeLogStore:
    logs: dict[str, str] = field(default_factory=dict)

    def read_log(self, ref: str) -> str | None:
        return self.logs.get(ref)


@dataclass
class FakePrincipalResolver:
    by_credential: dict[str, Principal] = field(default_factory=dict)

    def resolve(self, credential: str) -> Principal | None:
        return self.by_credential.get(credential)


@dataclass
class FakeAuthorizer:
    inactive_users: set[str] = field(default_factory=set)
    project_owners: dict[str, str] = field(default_factory=dict)  # project_id -> owner

    def authorization_active(self, user_id: str) -> bool:
        return user_id not in self.inactive_users

    def may_submit(self, user_id: str, project_id: str) -> bool:
        # DEP-03 is open; the baseline is "the owner may submit".
        return self.project_owners.get(project_id) == user_id


@dataclass
class FakeRebuildAttempts:
    attempts: dict[str, AttemptView] = field(default_factory=dict)

    def add(self, attempt: AttemptView) -> None:
        self.attempts[attempt.attempt_id] = attempt

    def get(self, attempt_id: str) -> AttemptView | None:
        return self.attempts.get(attempt_id)


@dataclass
class FakeSubmissionPort:
    submitted: list[SubmissionRequest] = field(default_factory=list)
    reject_reason: str | None = None  # set to simulate quota or validation failure

    def submit(self, request: SubmissionRequest) -> SubmissionReceipt:
        if self.reject_reason is not None:
            return SubmissionReceipt(accepted=False, reason=self.reject_reason)
        self.submitted.append(request)
        return SubmissionReceipt(accepted=True, job_id=f"job-{len(self.submitted)}")


@dataclass
class FakeAuditSink:
    events: list[AuditEvent] = field(default_factory=list)

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def operations(self) -> list[str]:
        return [e.operation for e in self.events]


@dataclass
class FakeAllocationHistory:
    records: list[AllocationRecord] = field(default_factory=list)

    def for_units(self, unit_ids: Sequence[str], since: datetime) -> Sequence[AllocationRecord]:
        wanted = set(unit_ids)
        return [
            r
            for r in self.records
            if r.unit_id in wanted and (r.ended_at is None or r.ended_at >= since)
        ]


@dataclass
class FakeLedgerHistory:
    entries: list[LedgerEntry] = field(default_factory=list)

    def for_user(self, owner_id: str, project_id: str | None, limit: int) -> Sequence[LedgerEntry]:
        mine = [
            e
            for e in self.entries
            if e.owner_id == owner_id and (project_id is None or e.project_id == project_id)
        ]
        return sorted(mine, key=lambda e: e.at, reverse=True)[:limit]


__all__ = [
    "FakeAllocationHistory",
    "FakeAuditSink",
    "FakeAuthorizer",
    "FakeLedgerHistory",
    "FakeLogStore",
    "FakePrincipalResolver",
    "FakeRebuildAttempts",
    "FakeRunReader",
    "FakeSubmissionPort",
]
