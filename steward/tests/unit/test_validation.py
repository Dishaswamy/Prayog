"""Server-side validation of Runtime output: NFR-INT-05, FR-GPU-161, FR-STW-017/018."""

from typing import Any

import pytest

from factories import REQUESTS, ok_response, s1_request, s2_request, s3_request, s4_request
from prayog_steward.coordinator.validation import validate_response
from steward_contracts import (
    Context,
    InvokeOutcome,
    InvokeResponse,
    Provenance,
    S1Result,
    S2Result,
    S3Turn,
    S4Result,
)
from steward_runtime import StubBehaviour, StubReasoning

# ---------------------------------------------------------------- NFR-INT-05: stub drives all four


@pytest.mark.parametrize("ctx", list(Context))
def test_nfr_int_05_valid_stub_response_passes(ctx: Context) -> None:
    request = REQUESTS[ctx]()
    v = validate_response(request, StubReasoning().invoke(request))
    assert v.outcome is InvokeOutcome.OK
    assert v.result is not None
    assert v.stripped == 0


@pytest.mark.parametrize("ctx", list(Context))
def test_nfr_int_05_malformed_response_is_rejected(ctx: Context) -> None:
    request = REQUESTS[ctx]()
    v = validate_response(request, StubReasoning(StubBehaviour.MALFORMED).invoke(request))
    assert v.outcome is InvokeOutcome.MALFORMED
    assert v.result is None
    assert not v.out_of_contract


@pytest.mark.parametrize("ctx", list(Context))
def test_nfr_int_05_write_attempt_is_rejected_and_flagged(ctx: Context) -> None:
    """FR-GPU-151: an 'action' smuggled into the result never reaches the server's writers."""
    request = REQUESTS[ctx]()
    v = validate_response(request, StubReasoning(StubBehaviour.WRITE_ATTEMPT).invoke(request))
    assert v.outcome is InvokeOutcome.MALFORMED
    assert v.out_of_contract
    assert v.result is None


@pytest.mark.parametrize("ctx", [Context.S1, Context.S2, Context.S4])
def test_nfr_int_05_uncited_claims_are_stripped(ctx: Context) -> None:
    request = REQUESTS[ctx]()
    v = validate_response(request, StubReasoning(StubBehaviour.UNCITED).invoke(request))
    assert v.outcome is InvokeOutcome.OK
    assert v.stripped >= 1


@pytest.mark.parametrize(
    "behaviour, outcome",
    [
        (StubBehaviour.REFUSE, InvokeOutcome.REFUSED),
        (StubBehaviour.UNAVAILABLE, InvokeOutcome.UNAVAILABLE),
    ],
)
def test_non_ok_outcomes_pass_through_without_result(
    behaviour: StubBehaviour, outcome: InvokeOutcome
) -> None:
    request = s1_request()
    v = validate_response(request, StubReasoning(behaviour).invoke(request))
    assert (v.outcome, v.result) == (outcome, None)


def test_response_for_another_invocation_is_rejected() -> None:
    request = s1_request()
    other = StubReasoning().invoke(s2_request())
    assert validate_response(request, other).outcome is InvokeOutcome.MALFORMED
    forged = InvokeResponse.model_validate(
        {**StubReasoning().invoke(request).model_dump(), "invocation_id": "inv-other"}
    )
    assert validate_response(request, forged).outcome is InvokeOutcome.MALFORMED


# ---------------------------------------------------------------- FR-GPU-161: S1 citation stripping


def _s1(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "classification": "oom",
        "claims": [],
        "matching_earlier_failures": [],
        "hardware_finding": {"verdict": "unlikely_hardware", "indicators": []},
        "proposal": None,
    }
    return {**base, **overrides}


def test_fr_gpu_161_claims_citing_unknown_or_no_records_are_stripped() -> None:
    request = s1_request()
    result = _s1(
        claims=[
            {"text": "OOM at batch 512", "cites": ["log:r1"]},
            {"text": "made up", "cites": []},
            {"text": "another user's run", "cites": ["run:someone-else"]},
            {"text": "half valid", "cites": ["log:r1", "run:invented"]},
        ]
    )
    v = validate_response(request, ok_response(request, result))
    assert isinstance(v.result, S1Result)
    assert [c.text for c in v.result.claims] == ["OOM at batch 512"]
    assert v.stripped == 3


def test_fr_gpu_148_matching_failures_must_be_earlier_runs_of_this_invocation() -> None:
    request = s1_request()
    result = _s1(matching_earlier_failures=["run:r0", "log:r1", "run:elsewhere"])
    v = validate_response(request, ok_response(request, result))
    assert isinstance(v.result, S1Result)
    assert v.result.matching_earlier_failures == ["run:r0"]


def test_proposal_changes_need_cited_reasons_and_empty_proposal_is_dropped() -> None:
    request = s1_request()
    cited = {
        "field": "batch_size",
        "new_value": "256",
        "reason": {"text": "OOM at 512", "cites": ["log:r1"]},
    }
    uncited = {"field": "gpu_count", "new_value": "8", "reason": {"text": "more!", "cites": []}}

    v = validate_response(
        request, ok_response(request, _s1(proposal={"changes": [cited, uncited]}))
    )
    assert isinstance(v.result, S1Result)
    assert v.result.proposal is not None
    assert [c.field.value for c in v.result.proposal.changes] == ["batch_size"]

    v = validate_response(request, ok_response(request, _s1(proposal={"changes": [uncited]})))
    assert isinstance(v.result, S1Result)
    assert v.result.proposal is None


def test_proposal_cannot_touch_fields_outside_the_allow_list() -> None:
    request = s1_request()
    bad = {"field": "priority", "new_value": "P0", "reason": {"text": "x", "cites": ["log:r1"]}}
    v = validate_response(request, ok_response(request, _s1(proposal={"changes": [bad]})))
    assert v.outcome is InvokeOutcome.MALFORMED


# ---------------------------------------------------------------- S2: only rows in the bundle


def test_fr_gpu_165_s2_rationale_and_rows_limited_to_the_bundle() -> None:
    request = s2_request()
    result = {
        "same_fault": True,
        "row_ids": ["ev0", "ev1", "ev-not-in-bundle"],
        "finding": {"verdict": "likely_hardware", "indicators": ["xid_error"]},
        "rationale": [
            {"text": "same unit within minutes", "cites": ["ev0", "ev1"]},
            {"text": "also row 99", "cites": ["ev99"]},
        ],
    }
    v = validate_response(request, ok_response(request, result))
    assert isinstance(v.result, S2Result)
    assert v.result.row_ids == ["ev0", "ev1"]
    assert len(v.result.rationale) == 1
    assert v.stripped == 2


# ---------------------------------------------------------------- S3: provenance (FR-STW-017/018)


def _claim(text: str, provenance: str, cites: list[str]) -> dict[str, Any]:
    return {"text": text, "provenance": provenance, "cites": cites}


def test_fr_stw_017_provenance_must_match_the_cited_records() -> None:
    request = s3_request()
    result = {
        "claims": [
            _claim("snapshot pins torch 2.3.1", "internal", ["led:1"]),
            _claim("x renamed in 2.4", "external", ["web:1"]),
            _claim("web result posing as ledger", "internal", ["web:1"]),
            _claim("ledger posing as web", "external", ["led:1"]),
            _claim("generally, pin versions", "reasoning", []),
            _claim("reasoning that cites", "reasoning", ["led:1"]),
        ],
        "steps": [{"explanation": "pin x", "command": "pip install x==2.3"}],
        "evidence_found": True,
    }
    v = validate_response(request, ok_response(request, result))
    assert isinstance(v.result, S3Turn)
    kept = [(c.text, c.provenance) for c in v.result.claims]
    assert kept == [
        ("snapshot pins torch 2.3.1", Provenance.INTERNAL),
        ("x renamed in 2.4", Provenance.EXTERNAL),
        ("generally, pin versions", Provenance.REASONING),
    ]
    assert v.result.evidence_found


def test_fr_stw_018_evidence_found_is_withdrawn_when_no_internal_claim_survives() -> None:
    request = s3_request(with_external=False)
    result = {
        "claims": [_claim("invented ledger fact", "internal", ["led:999"])],
        "steps": [],
        "evidence_found": True,
    }
    v = validate_response(request, ok_response(request, result))
    assert isinstance(v.result, S3Turn)
    assert v.result.claims == []
    assert v.result.evidence_found is False


def test_s4_suggestions_need_citations() -> None:
    request = s4_request()
    result = {
        "suggestions": [
            {"kind": "hyperparameter", "text": "lower lr", "cites": ["met:a2"]},
            {"kind": "architecture", "text": "use a bigger model", "cites": []},
        ]
    }
    v = validate_response(request, ok_response(request, result))
    assert isinstance(v.result, S4Result)
    assert [s.text for s in v.result.suggestions] == ["lower lr"]
