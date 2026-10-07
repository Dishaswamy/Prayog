"""The Steward Coordinator: the only writer of Steward results (SADD §8.2, SD-15, SD-16).

For each claimed task it builds scoped input through the Gateway, invokes the model
through the ReasoningPort with no transaction open, validates the response server-side
(ADR-S04) and persists the result. When the model is unavailable it retries with backoff.
After the last attempt it records an 'unavailable' diagnosis so the owner sees a clear
state rather than silence (NFR-AVAIL-001). Scheduling never waits on any of this
(NFR-GPU-055).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, Engine, select

from prayog_steward.audit import Auditor
from prayog_steward.clock import Clock
from prayog_steward.config import StewardSettings
from prayog_steward.coordinator import tasks
from prayog_steward.coordinator.proposals import apply_changes
from prayog_steward.coordinator.validation import Validated, validate_response
from prayog_steward.db.models import diagnoses, hardware_findings, hardware_warnings, proposals
from prayog_steward.gateway import Gateway
from prayog_steward.hardware import attach_advice
from prayog_steward.ports import RunEnd, RunReader, RunView
from steward_contracts import (
    Context,
    InvokeOutcome,
    InvokeRequest,
    InvokeResponse,
    ReasoningPort,
    RecordKind,
    S1Result,
    S2Result,
)

log = logging.getLogger("prayog_steward.coordinator")


class RetryLater(Exception):
    """The model was unavailable; try again after backoff."""


def cited(claims: list[Any], kinds: dict[str, RecordKind | None]) -> list[dict[str, Any]]:
    """Claims as stored and displayed: text plus each citation's ID and record kind."""
    return [
        {
            "text": c.text,
            "cites": [
                {"record_id": rid, "kind": (k.value if (k := kinds.get(rid)) else "evidence")}
                for rid in c.cites
            ],
        }
        for c in claims
    ]


def _kinds(request: InvokeRequest) -> dict[str, RecordKind | None]:
    payload = request.payload
    if hasattr(payload, "records"):
        return {r.record_id: r.kind for r in payload.records}
    return {r.row_id: None for r in payload.rows}


class Coordinator:
    def __init__(
        self,
        *,
        engine: Engine,
        settings: StewardSettings,
        clock: Clock,
        reasoning: ReasoningPort,
        gateway: Gateway,
        auditor: Auditor,
        runs: RunReader,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.clock = clock
        self.reasoning = reasoning
        self.gateway = gateway
        self.auditor = auditor
        self.runs = runs

    # ------------------------------------------------------------ loop

    def run_once(self) -> bool:
        """Claim and process one task. Returns False when there was nothing to do."""
        task = tasks.claim(self.engine, self.clock.now(), self.settings.task_lease)
        if task is None:
            return False
        try:
            if task.context is Context.S1:
                self._s1(task)
            else:
                self._s2(task)
        except RetryLater as exc:
            gave_up = tasks.retry_or_give_up(
                self.engine,
                task,
                self.clock.now(),
                str(exc),
                max_attempts=self.settings.task_max_attempts,
                base=self.settings.task_backoff_base,
            )
            if gave_up:
                self._gave_up(task)
            return True
        except Exception as exc:  # never let one bad task stop the worker
            log.exception("steward task %s failed", task.task_id)
            gave_up = tasks.retry_or_give_up(
                self.engine,
                task,
                self.clock.now(),
                f"{type(exc).__name__}",
                max_attempts=self.settings.task_max_attempts,
                base=self.settings.task_backoff_base,
            )
            if gave_up:
                self._gave_up(task)
            return True
        tasks.complete(self.engine, task, self.clock.now())
        return True

    def drain(self, limit: int = 1_000) -> int:
        """Process tasks until none are due (tests, demo, cron-style runs)."""
        done = 0
        while done < limit and self.run_once():
            done += 1
        return done

    def _invoke(self, request: InvokeRequest) -> tuple[InvokeResponse, Validated[BaseModel]]:
        response = self.reasoning.invoke(request)
        validated = validate_response(request, response)
        if validated.outcome is InvokeOutcome.UNAVAILABLE:
            raise RetryLater(validated.problem or "reasoning unavailable")
        if validated.out_of_contract:
            # FR-GPU-151: an attempted action under the Steward identity is refused and logged.
            self.auditor.by_steward(
                "steward.write.refused",
                request.invocation_id,
                outcome="refused",
                reason=validated.problem,
            )
        return response, validated

    # ------------------------------------------------------------ S1 (SD-15)

    def _s1(self, task: tasks.Task) -> None:
        run = self.runs.get_run(task.subject_id)
        if run is None or run.end is not RunEnd.FAILED:
            self.auditor.by_steward(
                "steward.diagnosis.skipped",
                task.subject_id,
                outcome="skipped",
                reason="run not found or not FAILED",
            )
            return
        with self.engine.connect() as conn:
            _, request = self.gateway.s1_request(conn, run, self.clock.now())
        response, validated = self._invoke(request)
        with self.engine.begin() as conn:
            diagnosis_id = self._store_s1(conn, run, request, response, validated)
        self.auditor.by_steward(
            "steward.diagnosis.recorded",
            run.run_id,
            str(diagnosis_id),
            outcome=validated.outcome.value,
            reason=f"stripped={validated.stripped}" if validated.stripped else validated.problem,
        )

    def _store_s1(
        self,
        conn: Connection,
        run: RunView,
        request: InvokeRequest,
        response: InvokeResponse,
        validated: Validated[BaseModel],
    ) -> uuid.UUID:
        now = self.clock.now()
        result = validated.result if isinstance(validated.result, S1Result) else None
        diagnosis_id = uuid.uuid4()
        conn.execute(
            diagnoses.insert().values(
                diagnosis_id=diagnosis_id,
                run_id=run.run_id,
                project_id=run.project_id,
                owner_id=run.owner_id,
                outcome=validated.outcome.value,
                classification=result.classification.value if result else None,
                claims=cited(result.claims, _kinds(request)) if result else [],
                matching_earlier_failures=list(result.matching_earlier_failures) if result else [],
                stripped_count=validated.stripped,
                agent_id=response.agent_id,
                agent_version=response.agent_version,
                model=response.model,
                created_at=now,
            )
        )
        if result is None:
            return diagnosis_id
        conn.execute(
            hardware_findings.insert().values(
                finding_id=uuid.uuid4(),
                diagnosis_id=diagnosis_id,
                run_id=run.run_id,
                verdict=result.hardware_finding.verdict.value,
                indicators=[i.value for i in result.hardware_finding.indicators],
                created_at=now,
            )
        )
        if result.proposal is not None:
            spec, applied = apply_changes(
                run.job_spec, list(result.proposal.changes), self.settings
            )
            if applied:
                proposal_id = uuid.uuid4()
                conn.execute(
                    proposals.insert().values(
                        proposal_id=proposal_id,
                        diagnosis_id=diagnosis_id,
                        run_id=run.run_id,
                        project_id=run.project_id,
                        owner_id=run.owner_id,
                        changes=[c.model_dump(mode="json") for c in applied],
                        job_spec=asdict(spec),
                        requested_gpu_hours=Decimal(str(round(spec.requested_gpu_hours, 2))),
                        state="PROPOSED",
                        created_at=now,
                    )
                )
                self.auditor.by_steward("steward.proposal.created", run.run_id, str(proposal_id))
        return diagnosis_id

    # ------------------------------------------------------------ S2 (SD-16)

    def _s2(self, task: tasks.Task) -> None:
        warning_id = uuid.UUID(task.subject_id)
        with self.engine.connect() as conn:
            state = conn.execute(
                select(hardware_warnings.c.state).where(
                    hardware_warnings.c.warning_id == warning_id
                )
            ).scalar()
            if state != "OPEN":
                return  # closed before we got to it; nothing to advise
            _, request = self.gateway.s2_request(conn, warning_id, self.clock.now())
        _, validated = self._invoke(request)
        if not isinstance(validated.result, S2Result):
            self.auditor.by_steward(
                "steward.correlation.recorded",
                task.subject_id,
                outcome=validated.outcome.value,
                reason=validated.problem,
            )
            return
        advice = validated.result.model_dump(mode="json")
        advice["rationale"] = cited(validated.result.rationale, _kinds(request))
        with self.engine.begin() as conn:
            stored = attach_advice(conn, warning_id, advice)
        self.auditor.by_steward(
            "steward.correlation.recorded",
            task.subject_id,
            outcome="ok" if stored else "warning_closed",
            reason=f"stripped={validated.stripped}" if validated.stripped else None,
        )

    # ------------------------------------------------------------ give-up

    def _gave_up(self, task: tasks.Task) -> None:
        """After the last attempt: a clear 'unavailable' record for S1 (NFR-AVAIL-001)."""
        self.auditor.by_steward(
            "steward.task.gave_up", task.context.value, task.subject_id, outcome="gave_up"
        )
        if task.context is not Context.S1:
            return
        run = self.runs.get_run(task.subject_id)
        if run is None:
            return
        with self.engine.begin() as conn:
            conn.execute(
                diagnoses.insert().values(
                    diagnosis_id=uuid.uuid4(),
                    run_id=run.run_id,
                    project_id=run.project_id,
                    owner_id=run.owner_id,
                    outcome=InvokeOutcome.UNAVAILABLE.value,
                    classification=None,
                    claims=[],
                    matching_earlier_failures=[],
                    stripped_count=0,
                    agent_id="prayog-steward",
                    agent_version="n/a",
                    model=None,
                    created_at=self.clock.now(),
                )
            )
