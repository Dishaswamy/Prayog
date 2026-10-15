"""Wires the real Steward services to fakes for the other teams' modules."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine

from factories import T0, run_view
from prayog_steward.audit import Auditor
from prayog_steward.config import StewardSettings
from prayog_steward.coordinator.service import Coordinator
from prayog_steward.fakes import (
    FakeAllocationHistory,
    FakeAuditSink,
    FakeAuthorizer,
    FakeClock,
    FakeLedgerHistory,
    FakeLogStore,
    FakeRebuildAttempts,
    FakeRunReader,
    FakeSubmissionPort,
)
from prayog_steward.gateway import Gateway
from prayog_steward.hooks import SealOutcome, on_run_sealed
from prayog_steward.owner import OwnerService
from prayog_steward.ports import Principal, PrincipalKind, RunEnd, RunView
from prayog_steward.rag.sanitiser import Sanitiser
from steward_runtime import StubReasoning


def settings(**overrides: Any) -> StewardSettings:
    base: dict[str, Any] = {"_env_file": None, "task_backoff_base": timedelta(seconds=1)}
    return StewardSettings(**{**base, **overrides})


def user(uid: str, *roles: str, session: str | None = None) -> Principal:
    return Principal(
        uid,
        PrincipalKind.USER,
        lab=f"lab-{uid}",
        roles=frozenset(roles),
        session_id=session or f"sess-{uid}",
    )


@dataclass
class Harness:
    engine: Engine
    settings: StewardSettings = field(default_factory=settings)
    clock: FakeClock = field(default_factory=lambda: FakeClock(T0 + timedelta(hours=1)))
    runs: FakeRunReader = field(default_factory=FakeRunReader)
    logs: FakeLogStore = field(default_factory=FakeLogStore)
    authorizer: FakeAuthorizer = field(default_factory=FakeAuthorizer)
    submissions: FakeSubmissionPort = field(default_factory=FakeSubmissionPort)
    audit: FakeAuditSink = field(default_factory=FakeAuditSink)
    allocations: FakeAllocationHistory = field(default_factory=FakeAllocationHistory)
    attempts: FakeRebuildAttempts = field(default_factory=FakeRebuildAttempts)
    ledger: FakeLedgerHistory = field(default_factory=FakeLedgerHistory)
    reasoning: StubReasoning = field(default_factory=StubReasoning)

    def __post_init__(self) -> None:
        self.auditor = Auditor(self.audit, self.clock, self.settings.service_principal_id)
        self.sanitiser = Sanitiser()
        self.gateway = Gateway(
            runs=self.runs,
            logs=self.logs,
            sanitiser=self.sanitiser,
            settings=self.settings,
            auditor=self.auditor,
        )
        self.coordinator = Coordinator(
            engine=self.engine,
            settings=self.settings,
            clock=self.clock,
            reasoning=self.reasoning,
            gateway=self.gateway,
            auditor=self.auditor,
            runs=self.runs,
        )
        self.owner = OwnerService(
            engine=self.engine,
            runs=self.runs,
            authorizer=self.authorizer,
            submissions=self.submissions,
            auditor=self.auditor,
            clock=self.clock,
        )

    def seal(self, run: RunView, log_tail: str | None = None) -> SealOutcome:
        """What the platform's Results Service does, in its own transaction."""
        self.runs.add(run)
        self.authorizer.project_owners.setdefault(run.project_id, run.owner_id)
        with self.engine.begin() as conn:
            return on_run_sealed(
                conn, run, settings=self.settings, clock=self.clock, log_tail=log_tail
            )

    def failed_run(
        self,
        run_id: str,
        *,
        owner: str = "alice",
        project: str = "p1",
        units: tuple[str, ...] = ("n1/gpu0",),
        log: str | None = None,
        batch_size: int | None = None,
        end: RunEnd = RunEnd.FAILED,
        minutes_ago: int = 5,
        node: str = "n1",
    ) -> RunView:
        run = run_view(
            run_id,
            owner_id=owner,
            project_id=project,
            end=end,
            ended_at=self.clock.now() - timedelta(minutes=minutes_ago),
        )
        refs: tuple[str, ...] = ()
        if log is not None:
            self.logs.logs[f"logs/{run_id}"] = log
            refs = (f"logs/{run_id}",)
        return replace(
            run,
            unit_ids=units,
            node_id=node,
            log_refs=refs,
            owner_lab=f"lab-{owner}",
            failure_category="CRASH" if end is RunEnd.FAILED else None,
            job_spec=replace(run.job_spec, batch_size=batch_size),
        )
