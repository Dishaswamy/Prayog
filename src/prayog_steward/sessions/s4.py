"""S4: optimisation advisory after a successful rebuild (FR-STW-006, UC-STW-03).

Allowed only for the owner's attempt in exactly REBUILD_SUCCESS (A05, A06, A15, A27).
Input is that run's record and telemetry plus the owner's own ledger entries for the
project (ADR-S08 "permitted metrics"), all sanitised. The output is advisory text only.
Nothing is applied or executed (A08), and denials are audited (NFR-SEC-007).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from prayog_steward.audit import Auditor
from prayog_steward.config import StewardSettings
from prayog_steward.coordinator.validation import validate_response
from prayog_steward.gateway import Gateway
from prayog_steward.ports import LedgerHistory, Principal, RebuildAttempts, RebuildState, RunReader
from prayog_steward.sessions.links import AccessDenied, Denial
from steward_contracts import (
    InvokeOutcome,
    InvokeRequest,
    ReasoningPort,
    RecordKind,
    S4Input,
    S4Result,
)

PERMITTED_LEDGER_KINDS = {"metrics", "config", "hyperparameters", "run_summary"}


@dataclass(frozen=True)
class S4Answer:
    outcome: str
    attempt_id: str
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


class OptimizeService:
    def __init__(
        self,
        *,
        attempts: RebuildAttempts,
        runs: RunReader,
        ledger: LedgerHistory,
        gateway: Gateway,
        reasoning: ReasoningPort,
        auditor: Auditor,
        settings: StewardSettings,
    ) -> None:
        self.attempts = attempts
        self.runs = runs
        self.ledger = ledger
        self.gateway = gateway
        self.reasoning = reasoning
        self.auditor = auditor
        self.settings = settings

    def optimize(self, principal: Principal, attempt_id: str) -> S4Answer:
        attempt = self.attempts.get(attempt_id)
        if attempt is None or attempt.owner_id != principal.principal_id:
            self.auditor.by(
                principal, "steward.optimize", attempt_id, outcome="denied", reason="not owner"
            )
            raise AccessDenied(Denial.NOT_FOUND, "No such rebuild attempt.")
        if attempt.state is not RebuildState.SUCCESS:
            self.auditor.by(
                principal,
                "steward.optimize",
                attempt_id,
                outcome="denied",
                reason=f"state {attempt.state.value}",
            )
            raise AccessDenied(
                Denial.WRONG_STATE,
                f"prayog steward optimize requires REBUILD_SUCCESS; this attempt is "
                f"{attempt.state.value}.",
            )
        records = []
        run = self.runs.get_run(attempt.run_id) if attempt.run_id else None
        if run is not None and run.owner_id == principal.principal_id:
            records.append(self.gateway.run_record(run))
            telemetry = self.runs.telemetry(run.run_id)
            if telemetry is not None:
                records.append(
                    self.gateway.record(
                        f"telemetry:{run.run_id}", RecordKind.TELEMETRY, telemetry.summary
                    )
                )
        for entry in self.ledger.for_user(
            principal.principal_id, attempt.project_id, self.settings.ledger_entries_limit
        ):
            if entry.owner_id != principal.principal_id or entry.kind not in PERMITTED_LEDGER_KINDS:
                continue
            kind = RecordKind.METRICS if entry.kind == "metrics" else RecordKind.CONFIG
            records.append(self.gateway.record(f"led:{entry.entry_id}", kind, entry.body))

        request = InvokeRequest(
            invocation_id=str(uuid.uuid4()),
            payload=S4Input(attempt_id=attempt_id, records=records),
        )
        validated = validate_response(request, self.reasoning.invoke(request))
        if validated.out_of_contract:
            self.auditor.by_steward(
                "steward.write.refused",
                request.invocation_id,
                outcome="refused",
                reason=validated.problem,
            )
        self.auditor.by(principal, "steward.optimize", attempt_id, outcome=validated.outcome.value)
        if not isinstance(validated.result, S4Result):
            notice = (
                "Steward is temporarily unavailable; please try again shortly."
                if validated.outcome is InvokeOutcome.UNAVAILABLE
                else "Steward could not produce usable suggestions."
            )
            return S4Answer(validated.outcome.value, attempt_id, notices=[notice])
        notices = ["These suggestions are advisory. Nothing has been changed or run."]
        if not validated.result.suggestions:
            notices.append("No evidence-backed suggestions were found for this run.")
        return S4Answer(
            "ok",
            attempt_id,
            suggestions=[s.model_dump(mode="json") for s in validated.result.suggestions],
            notices=notices,
        )
