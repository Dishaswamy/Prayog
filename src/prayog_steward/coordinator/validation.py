"""Server-side validation of everything the Runtime returns (ADR-S04).

The server never trusts the Runtime. A response is first validated against the
context's contract; unknown fields (an attempted action or write) make it MALFORMED
(NFR-INT-05). Then every claim must cite records that were handed out *in the same
invocation*; anything else is stripped before storage or display (FR-GPU-161,
FR-STW-017, 018). The scope comes from the request itself, so it cannot be widened.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from steward_contracts import (
    RESULT_TYPES,
    Claim,
    Context,
    InvokeOutcome,
    InvokeRequest,
    InvokeResponse,
    JobChange,
    Provenance,
    RecordKind,
    S1Input,
    S1Result,
    S2Input,
    S2Result,
    S3Input,
    S3Turn,
    S4Input,
    S4Result,
    SourcedClaim,
)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Validated(Generic[T]):
    outcome: InvokeOutcome
    result: T | None
    stripped: int = 0  # claims, citations or changes removed by validation
    out_of_contract: bool = False  # unknown fields present: treat as an attempted write
    problem: str | None = None  # payload-free summary, safe for the audit log


def _scope(request: InvokeRequest) -> dict[str, RecordKind | None]:
    """Record IDs handed to the model in this invocation, mapped to their kind.
    S2 evidence rows have no RecordKind and map to None."""
    p = request.payload
    if isinstance(p, S2Input):
        return {row.row_id: None for row in p.rows}
    assert isinstance(p, S1Input | S3Input | S4Input)
    return {r.record_id: r.kind for r in p.records}


def _cited_in_scope(cites: list[str], scope: dict[str, RecordKind | None]) -> bool:
    return bool(cites) and all(c in scope for c in cites)


def _keep_claims(claims: list[Claim], scope: dict[str, RecordKind | None]) -> list[Claim]:
    return [c for c in claims if _cited_in_scope(c.cites, scope)]


def _sourced_claim_ok(claim: SourcedClaim, scope: dict[str, RecordKind | None]) -> bool:
    if claim.provenance is Provenance.REASONING:
        # General reasoning is allowed uncited but must not pose as evidence (FR-STW-018).
        return not claim.cites
    if not _cited_in_scope(claim.cites, scope):
        return False
    kinds = {scope[c] for c in claim.cites}
    if claim.provenance is Provenance.EXTERNAL:
        return kinds == {RecordKind.EXTERNAL}
    if claim.provenance is Provenance.LITERATURE:
        return kinds == {RecordKind.REFERENCE}
    # Internal claims are about the user's own records, never the web or the literature.
    return not kinds & {RecordKind.EXTERNAL, RecordKind.REFERENCE}


def _strip_s1(result: S1Result, scope: dict[str, RecordKind | None]) -> tuple[S1Result, int]:
    claims = _keep_claims(result.claims, scope)
    earlier = [
        rid for rid in result.matching_earlier_failures if scope.get(rid) is RecordKind.EARLIER_RUN
    ]
    proposal = result.proposal
    removed = len(result.claims) - len(claims)
    removed += len(result.matching_earlier_failures) - len(earlier)
    if proposal is not None:
        changes: list[JobChange] = [
            ch for ch in proposal.changes if _cited_in_scope(ch.reason.cites, scope)
        ]
        removed += len(proposal.changes) - len(changes)
        proposal = proposal.model_copy(update={"changes": changes}) if changes else None
    return (
        result.model_copy(
            update={"claims": claims, "matching_earlier_failures": earlier, "proposal": proposal}
        ),
        removed,
    )


def _strip_s2(result: S2Result, scope: dict[str, RecordKind | None]) -> tuple[S2Result, int]:
    rows = [r for r in result.row_ids if r in scope]
    rationale = _keep_claims(result.rationale, scope)
    removed = (len(result.row_ids) - len(rows)) + (len(result.rationale) - len(rationale))
    return result.model_copy(update={"row_ids": rows, "rationale": rationale}), removed


def _strip_s3(result: S3Turn, scope: dict[str, RecordKind | None]) -> tuple[S3Turn, int]:
    claims = [c for c in result.claims if _sourced_claim_ok(c, scope)]
    # evidence_found may only be claimed if an internal claim survived (FR-STW-018).
    found = result.evidence_found and any(c.provenance is Provenance.INTERNAL for c in claims)
    removed = len(result.claims) - len(claims) + int(result.evidence_found and not found)
    return result.model_copy(update={"claims": claims, "evidence_found": found}), removed


def _strip_s4(result: S4Result, scope: dict[str, RecordKind | None]) -> tuple[S4Result, int]:
    kept = [s for s in result.suggestions if _cited_in_scope(s.cites, scope)]
    return result.model_copy(update={"suggestions": kept}), len(result.suggestions) - len(kept)


def validate_response(request: InvokeRequest, response: InvokeResponse) -> Validated[BaseModel]:
    ctx = request.context
    if response.invocation_id != request.invocation_id or response.context is not ctx:
        return Validated(InvokeOutcome.MALFORMED, None, problem="response does not match request")
    if response.outcome is not InvokeOutcome.OK:
        return Validated(response.outcome, None, problem=response.detail)
    if response.result is None:
        return Validated(InvokeOutcome.MALFORMED, None, problem="ok outcome without a result")

    try:
        parsed = RESULT_TYPES[ctx].model_validate(response.result)
    except ValidationError as exc:
        extra = any(err["type"] == "extra_forbidden" for err in exc.errors())
        problem = "unknown fields in result" if extra else f"{exc.error_count()} contract errors"
        return Validated(InvokeOutcome.MALFORMED, None, out_of_contract=extra, problem=problem)

    scope = _scope(request)
    stripped: BaseModel
    if ctx is Context.S1:
        assert isinstance(parsed, S1Result)
        stripped, removed = _strip_s1(parsed, scope)
    elif ctx is Context.S2:
        assert isinstance(parsed, S2Result)
        stripped, removed = _strip_s2(parsed, scope)
    elif ctx is Context.S3:
        assert isinstance(parsed, S3Turn)
        stripped, removed = _strip_s3(parsed, scope)
    else:
        assert isinstance(parsed, S4Result)
        stripped, removed = _strip_s4(parsed, scope)
    return Validated(InvokeOutcome.OK, stripped, stripped=removed)
