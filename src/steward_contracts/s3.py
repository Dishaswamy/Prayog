"""S3 — interactive rebuild diagnosis over the RAG pipeline (FR-STW-003…005, 010…018)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from steward_contracts.base import Context, Frozen, Record


class Provenance(StrEnum):
    """Every statement says where it came from (FR-STW-017, 018)."""

    INTERNAL = "internal"  # the user's own ML Flow / ledger evidence
    EXTERNAL = "external"  # web results: never presented as guaranteed fact
    REASONING = "reasoning"  # general reasoning over the error, no evidence cited


class SourcedClaim(Frozen):
    text: str
    provenance: Provenance
    cites: list[str]


class RemediationStep(Frozen):
    """A manual step. ``command`` is displayed as text and never executed (FR-STW-013, 014)."""

    explanation: str
    command: str | None


class SearchStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"  # A20: say so and use local context
    DISABLED = "disabled"


class S3Exchange(Frozen):
    question: str
    answer: str


class S3Input(Frozen):
    context: Literal[Context.S3] = Context.S3
    attempt_id: str
    question: str
    records: list[Record]
    history: list[S3Exchange]
    search_status: SearchStatus


class S3Turn(Frozen):
    claims: list[SourcedClaim]
    steps: list[RemediationStep]
    # False means no ledger evidence was found; nothing is fabricated (FR-STW-018).
    evidence_found: bool
