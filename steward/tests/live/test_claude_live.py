"""Live smoke test against the real Claude API. Costs a few cents; never runs in CI.

ANTHROPIC_API_KEY=... python -m pytest -m live_llm tests/live
"""

from __future__ import annotations

import os

import pytest

from factories import s1_request
from prayog_steward.coordinator.validation import validate_response
from steward_contracts import InvokeOutcome, Record, RecordKind, S1Result
from steward_runtime.claude import ClaudeReasoning

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no ANTHROPIC_API_KEY"),
]


def test_s1_oom_is_diagnosed_with_valid_citations() -> None:
    request = s1_request(
        Record(
            record_id="log:r1:1",
            kind=RecordKind.LOG_EXCERPT,
            body="torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 4.00 GiB"
            "\nbatch_size=512",
        ),
    )
    response = ClaudeReasoning(effort="low").invoke(request)
    validated = validate_response(request, response)
    assert validated.outcome is InvokeOutcome.OK, response.detail
    assert isinstance(validated.result, S1Result)
    assert validated.result.classification.value == "oom"
    assert validated.result.claims, "expected at least one cited claim to survive validation"
