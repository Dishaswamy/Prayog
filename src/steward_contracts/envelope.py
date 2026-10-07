"""The single operation between server and Runtime (ADR-S01, ADR-S02)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Protocol

from pydantic import Field

from steward_contracts.base import Context, Frozen
from steward_contracts.s1 import S1Input, S1Result
from steward_contracts.s2 import S2Input, S2Result
from steward_contracts.s3 import S3Input, S3Turn
from steward_contracts.s4 import S4Input, S4Result

StewardInput = Annotated[S1Input | S2Input | S3Input | S4Input, Field(discriminator="context")]
StewardResult = S1Result | S2Result | S3Turn | S4Result

RESULT_TYPES: dict[Context, type[S1Result] | type[S2Result] | type[S3Turn] | type[S4Result]] = {
    Context.S1: S1Result,
    Context.S2: S2Result,
    Context.S3: S3Turn,
    Context.S4: S4Result,
}


class InvokeOutcome(StrEnum):
    OK = "ok"
    REFUSED = "refused"  # model declined (stop_reason "refusal")
    MALFORMED = "malformed"  # output failed the contract
    UNAVAILABLE = "unavailable"  # model or network down (NFR-GPU-055, FR-STW-010)


class InvokeRequest(Frozen):
    invocation_id: str
    payload: StewardInput

    @property
    def context(self) -> Context:
        return self.payload.context


class InvokeResponse(Frozen):
    """``result`` is a raw dict on purpose. The server validates it against
    ``RESULT_TYPES[context]`` and does not trust the Runtime's own validation, so an
    out-of-contract response (e.g. an attempted write) is caught server-side (NFR-INT-05)."""

    invocation_id: str
    context: Context
    outcome: InvokeOutcome
    result: dict[str, Any] | None
    agent_id: str
    agent_version: str
    model: str | None = None
    detail: str | None = None  # short, payload-free reason for non-OK outcomes


class ReasoningPort(Protocol):
    """Implemented by ``steward_runtime`` (Claude, stub) and by the server's HTTP client."""

    def invoke(self, request: InvokeRequest) -> InvokeResponse: ...
