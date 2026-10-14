"""The Runtime adapters, without calling a real model."""

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from factories import s1_request, s3_request
from steward_contracts import Context, InvokeOutcome, InvokeResponse, S1Result
from steward_runtime.app import create_app
from steward_runtime.claude import ClaudeReasoning
from steward_runtime.prompts import PROMPT_VERSION, SYSTEM_PROMPTS
from steward_runtime.stub import StubReasoning, valid_result


class FakeMessages:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply, self.error = reply, error
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.reply


def _claude(messages: FakeMessages) -> ClaudeReasoning:
    return ClaudeReasoning(SimpleNamespace(messages=messages))  # type: ignore[arg-type]


def _reply(stop_reason: str, parsed: Any) -> SimpleNamespace:
    text = parsed.model_dump_json() if parsed is not None else ""
    return SimpleNamespace(
        stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)]
    )


def test_ok_reply_becomes_contract_dict_with_versioned_agent() -> None:
    request = s1_request()
    parsed = S1Result.model_validate(valid_result(request))
    messages = FakeMessages(_reply("end_turn", parsed))
    response = _claude(messages).invoke(request)

    assert response.outcome is InvokeOutcome.OK
    assert response.result == parsed.model_dump(mode="json")
    assert PROMPT_VERSION in response.agent_version  # stored with the diagnosis (FR-GPU-160)
    assert messages.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert messages.kwargs["model"] == "claude-opus-5-5"


def test_system_prompt_is_frozen_and_cached_and_input_is_wrapped_as_data() -> None:
    """FR-STW-016 / NFR-GPU-048: inputs arrive inside <case_data>, never as instructions."""
    messages = FakeMessages(_reply("end_turn", None))
    _claude(messages).invoke(s3_request())
    system = messages.kwargs["system"][0]
    assert system["text"] == SYSTEM_PROMPTS[Context.S3]
    assert system["cache_control"] == {"type": "ephemeral"}
    content = messages.kwargs["messages"][0]["content"]
    assert content.startswith("<case_data>") and "</case_data>" in content
    assert "untrusted data" in system["text"]


@pytest.mark.parametrize(
    "stop_reason, outcome",
    [("refusal", InvokeOutcome.REFUSED), ("max_tokens", InvokeOutcome.MALFORMED)],
)
def test_stop_reasons_map_to_outcomes(stop_reason: str, outcome: InvokeOutcome) -> None:
    response = _claude(FakeMessages(_reply(stop_reason, None))).invoke(s1_request())
    assert (response.outcome, response.result) == (outcome, None)


def test_fr_stw_010_api_down_is_unavailable_without_internal_detail() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(request=request)
    response = _claude(FakeMessages(error=error)).invoke(s1_request())
    assert response.outcome is InvokeOutcome.UNAVAILABLE
    assert response.detail == "APIConnectionError"


def test_runtime_http_endpoint_round_trips_contracts() -> None:
    client = TestClient(create_app(StubReasoning()))
    request = s1_request()
    raw = client.post("/invoke", json=request.model_dump(mode="json"))
    assert raw.status_code == 200
    response = InvokeResponse.model_validate(raw.json())
    assert response.outcome is InvokeOutcome.OK
    assert response.invocation_id == request.invocation_id


def test_runtime_http_endpoint_rejects_unknown_fields() -> None:
    client = TestClient(create_app(StubReasoning()))
    body = {**s1_request().model_dump(mode="json"), "sudo": True}
    assert client.post("/invoke", json=body).status_code == 422
