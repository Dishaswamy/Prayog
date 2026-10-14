"""ClaudeReasoning: one structured Messages API call per invocation (ADR-S02).

Input arrives already scoped and sanitised by the server (ADR-S01, ADR-S03). Output is
constrained by structured outputs to the context's contract, then re-validated by the
server, which does not trust this adapter either (ADR-S04).
"""

from __future__ import annotations

from typing import Any, Literal

import anthropic
from pydantic import ValidationError

from steward_contracts import RESULT_TYPES, InvokeOutcome, InvokeRequest, InvokeResponse
from steward_runtime.prompts import PROMPT_VERSION, SYSTEM_PROMPTS, render_case

AGENT_ID = "prayog-steward"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
Effort = Literal["low", "medium", "high", "xhigh", "max"]

# Structured-output formats, built once with the SDK's own schema transform.
_FORMATS: dict[Any, Any] = {
    ctx: {"type": "json_schema", "schema": anthropic.transform_schema(t.model_json_schema())}
    for ctx, t in RESULT_TYPES.items()
}


class ClaudeReasoning:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        model: str = "claude-opus-5-5",
        effort: Effort = "medium",
        max_tokens: int = 16_000,
        use_fallbacks: bool = True,
    ) -> None:
        self._client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.use_fallbacks = use_fallbacks

    @property
    def agent_version(self) -> str:
        return f"claude:{self.model}:prompt-{PROMPT_VERSION}"

    def _response(
        self,
        request: InvokeRequest,
        outcome: InvokeOutcome,
        result: dict[str, Any] | None = None,
        detail: str | None = None,
    ) -> InvokeResponse:
        return InvokeResponse(
            invocation_id=request.invocation_id,
            context=request.context,
            outcome=outcome,
            result=result,
            agent_id=AGENT_ID,
            agent_version=self.agent_version,
            model=self.model,
            detail=detail,
        )

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        ctx = request.context
        extra: dict[str, Any] = {}
        if self.use_fallbacks:
            # On a policy decline the API re-runs the request on a fallback model.
            extra = {
                "extra_headers": {"anthropic-beta": _FALLBACK_BETA},
                "extra_body": {"fallbacks": "default"},
            }
        result_type = RESULT_TYPES[ctx]
        try:
            # messages.create (not .parse): a refusal must be recognised from stop_reason
            # before anything tries to parse the (empty) text.
            message = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # Frozen per-context prompt, cached across invocations.
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPTS[ctx],
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": render_case(request.payload.model_dump_json(indent=1)),
                    }
                ],
                output_config={"effort": self.effort, "format": _FORMATS[ctx]},
                **extra,
            )
        except (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ) as exc:
            return self._response(request, InvokeOutcome.UNAVAILABLE, detail=type(exc).__name__)
        except anthropic.APIStatusError as exc:
            # Other API errors (e.g. overloaded 529, bad request 400): never retried as data.
            return self._response(
                request, InvokeOutcome.UNAVAILABLE, detail=f"api status {exc.status_code}"
            )

        if message.stop_reason == "refusal":
            return self._response(request, InvokeOutcome.REFUSED, detail="model refusal")
        if message.stop_reason == "max_tokens":
            return self._response(request, InvokeOutcome.MALFORMED, detail="output truncated")
        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            return self._response(request, InvokeOutcome.MALFORMED, detail="no text output")
        try:
            parsed = result_type.model_validate_json(text)
        except ValidationError:
            return self._response(request, InvokeOutcome.MALFORMED, detail="output failed schema")
        return self._response(request, InvokeOutcome.OK, result=parsed.model_dump(mode="json"))
