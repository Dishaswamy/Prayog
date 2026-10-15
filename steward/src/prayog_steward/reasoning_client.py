"""The server's ReasoningPort: an HTTP client for the separately deployed Runtime
(SADD ADR-02, ADR-S01). Any transport failure becomes an UNAVAILABLE response, which the
Coordinator retries and S3/S4 report clearly (NFR-GPU-055, FR-STW-010)."""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from steward_contracts import InvokeOutcome, InvokeRequest, InvokeResponse


class HttpReasoning:
    def __init__(
        self, base_url: str, timeout: float = 120.0, client: httpx.Client | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    def _unavailable(self, request: InvokeRequest, detail: str) -> InvokeResponse:
        return InvokeResponse(
            invocation_id=request.invocation_id,
            context=request.context,
            outcome=InvokeOutcome.UNAVAILABLE,
            result=None,
            agent_id="prayog-steward",
            agent_version="unreachable",
            detail=detail,
        )

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        try:
            response = self._client.post(
                f"{self.base_url}/invoke",
                content=request.model_dump_json(),
                headers={"content-type": "application/json"},
            )
        except httpx.HTTPError as exc:
            return self._unavailable(request, type(exc).__name__)
        if response.status_code != 200:
            return self._unavailable(request, f"runtime status {response.status_code}")
        try:
            return InvokeResponse.model_validate_json(response.content)
        except ValidationError:
            # A response that is not even an envelope; the Coordinator treats it as malformed.
            return InvokeResponse(
                invocation_id=request.invocation_id,
                context=request.context,
                outcome=InvokeOutcome.MALFORMED,
                result=None,
                agent_id="prayog-steward",
                agent_version="unknown",
                detail="runtime returned an invalid envelope",
            )

    def healthy(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/health", timeout=3.0).status_code == 200
        except httpx.HTTPError:
            return False
