"""ClaudeReasoning against the real Anthropic SDK, with the HTTP layer mocked.

Proves offline what the live API will receive: model, effort, the cached system prompt,
the structured-output schema for each context, the fallback beta, and that the parsed
response comes back through our contract.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2 as httpx
import pytest

from factories import REQUESTS
from prayog_steward.coordinator.validation import validate_response
from steward_contracts import RESULT_TYPES, Context, InvokeOutcome
from steward_runtime.claude import ClaudeReasoning
from steward_runtime.stub import valid_result


def _reply(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def _adapter(handler: Any) -> tuple[ClaudeReasoning, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(record)),
    )
    return ClaudeReasoning(client, effort="high"), seen


@pytest.mark.parametrize("ctx", list(Context))
def test_request_shape_and_round_trip_for_every_context(ctx: Context) -> None:
    request = REQUESTS[ctx]()
    answer = json.dumps(valid_result(request))
    adapter, seen = _adapter(lambda r: httpx.Response(200, json=_reply(answer)))

    response = adapter.invoke(request)

    assert response.outcome is InvokeOutcome.OK
    assert validate_response(request, response).outcome is InvokeOutcome.OK
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "high"
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["type"] == "object"
    assert set(fmt["schema"]["properties"]) == set(RESULT_TYPES[ctx].model_fields)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][0]["content"].startswith("<case_data>")
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert "thinking" not in body  # Opus 5.5: adaptive by default, never disabled


def test_refusal_and_truncation_map_to_outcomes() -> None:
    request = REQUESTS[Context.S1]()
    adapter, _ = _adapter(lambda r: httpx.Response(200, json=_reply("", "refusal")))
    assert adapter.invoke(request).outcome is InvokeOutcome.REFUSED


def test_api_errors_are_unavailable_not_exceptions() -> None:
    request = REQUESTS[Context.S1]()
    for status in (429, 500, 529, 400):
        adapter, _ = _adapter(
            lambda r, s=status: httpx.Response(
                s, json={"type": "error", "error": {"type": "x", "message": "x"}}
            )
        )
        assert adapter.invoke(request).outcome is InvokeOutcome.UNAVAILABLE


def test_output_that_breaks_the_schema_is_malformed() -> None:
    request = REQUESTS[Context.S1]()
    adapter, _ = _adapter(lambda r: httpx.Response(200, json=_reply('{"claims": 3}')))
    assert adapter.invoke(request).outcome is InvokeOutcome.MALFORMED
