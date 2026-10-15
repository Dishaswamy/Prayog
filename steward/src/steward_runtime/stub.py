"""StubReasoning: drives all four contexts without calling a model (NFR-INT-05).

Behaviours cover the cases the spec requires to be testable: a fixed valid response, a
malformed one, an uncited claim, an attempted write, a refusal and unavailability.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from steward_contracts import (
    Context,
    InvokeOutcome,
    InvokeRequest,
    InvokeResponse,
    S1Input,
    S2Input,
    S3Input,
    S4Input,
)

STUB_AGENT_VERSION = "stub-1"


class StubBehaviour(StrEnum):
    VALID = "valid"
    UNCITED = "uncited"  # a valid shape whose claims cite nothing
    MALFORMED = "malformed"  # missing required fields
    WRITE_ATTEMPT = "write_attempt"  # valid result plus an "action" the server must refuse
    REFUSE = "refuse"
    UNAVAILABLE = "unavailable"


def _first_ids(request: InvokeRequest, n: int = 1) -> list[str]:
    p = request.payload
    if isinstance(p, S2Input):
        return [r.row_id for r in p.rows[:n]]
    assert isinstance(p, S1Input | S3Input | S4Input)
    return [r.record_id for r in p.records[:n]]


def valid_result(request: InvokeRequest) -> dict[str, Any]:
    """A minimal, contract-valid result that cites the first record handed in."""
    cites = _first_ids(request)
    ctx = request.context
    if ctx is Context.S1:
        return {
            "classification": "unknown",
            "claims": [{"text": "Stub diagnosis.", "cites": cites}],
            "matching_earlier_failures": [],
            "hardware_finding": {"verdict": "inconclusive", "indicators": []},
            "proposal": None,
        }
    if ctx is Context.S2:
        p = request.payload
        assert isinstance(p, S2Input)
        return {
            "same_fault": True,
            "row_ids": [r.row_id for r in p.rows],
            "finding": {"verdict": "likely_hardware", "indicators": ["repeated_on_same_unit"]},
            "rationale": [{"text": "Stub correlation.", "cites": cites}],
        }
    if ctx is Context.S3:
        return {
            "claims": [{"text": "Stub answer.", "provenance": "internal", "cites": cites}]
            if cites
            else [{"text": "No evidence available.", "provenance": "reasoning", "cites": []}],
            "steps": [{"explanation": "Stub step.", "command": None}],
            "evidence_found": bool(cites),
        }
    return {"suggestions": [{"kind": "hyperparameter", "text": "Stub.", "cites": cites}]}


class StubReasoning:
    """Returns ``fixed[context]`` if given, else a result shaped by ``behaviour``."""

    def __init__(
        self,
        behaviour: StubBehaviour = StubBehaviour.VALID,
        fixed: Mapping[Context, dict[str, Any]] | None = None,
    ) -> None:
        self.behaviour = behaviour
        self.fixed = dict(fixed or {})
        self.calls: list[InvokeRequest] = []

    def _respond(
        self, request: InvokeRequest, outcome: InvokeOutcome, result: dict[str, Any] | None
    ) -> InvokeResponse:
        return InvokeResponse(
            invocation_id=request.invocation_id,
            context=request.context,
            outcome=outcome,
            result=result,
            agent_id="prayog-steward",
            agent_version=STUB_AGENT_VERSION,
            model=None,
            detail=None if outcome is InvokeOutcome.OK else f"stub:{self.behaviour.value}",
        )

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        self.calls.append(request)
        if request.context in self.fixed:
            return self._respond(request, InvokeOutcome.OK, self.fixed[request.context])
        b = self.behaviour
        if b is StubBehaviour.REFUSE:
            return self._respond(request, InvokeOutcome.REFUSED, None)
        if b is StubBehaviour.UNAVAILABLE:
            return self._respond(request, InvokeOutcome.UNAVAILABLE, None)
        result = valid_result(request)
        if b is StubBehaviour.MALFORMED:
            result = {}  # every required field missing, no unknown ones
        elif b is StubBehaviour.UNCITED:
            result = _uncite(result)
        elif b is StubBehaviour.WRITE_ATTEMPT:
            result = {**result, "action": {"type": "update_quota", "lab": "any", "hours": 1e6}}
        return self._respond(request, InvokeOutcome.OK, result)


def _uncite(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ([] if k == "cites" else _uncite(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_uncite(v) for v in value]
    return value
