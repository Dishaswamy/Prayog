"""HTTP face of the Runtime: ``POST /invoke``. Run with ``uvicorn steward_runtime.app:app``.

Configured from the environment only, since it may not import server code (ADR-S01):
``STEWARD_REASONING`` (claude | heuristic | stub), ``STEWARD_MODEL``, ``STEWARD_EFFORT``,
``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from steward_contracts import InvokeRequest, InvokeResponse, ReasoningPort
from steward_runtime.stub import StubReasoning


def build_reasoning() -> ReasoningPort:
    """Unset means ``heuristic``. The stub must be asked for by name, and an unknown value
    stops startup: either one silently in production would store placeholder diagnoses."""
    mode = os.environ.get("STEWARD_REASONING", "").strip().lower() or "heuristic"
    if mode not in ("claude", "heuristic", "stub"):
        raise ValueError(f"STEWARD_REASONING must be claude, heuristic or stub, not {mode!r}")
    if mode == "claude":
        from typing import get_args

        from steward_runtime.claude import ClaudeReasoning, Effort

        effort = os.environ.get("STEWARD_EFFORT", "medium")
        if effort not in get_args(Effort):
            raise ValueError(f"STEWARD_EFFORT must be one of {get_args(Effort)}")
        return ClaudeReasoning(
            model=os.environ.get("STEWARD_MODEL", "claude-opus-5-5"),
            effort=effort,  # type: ignore[arg-type]  # checked against Effort above
        )
    if mode == "heuristic":
        from steward_runtime.heuristic import HeuristicReasoning

        return HeuristicReasoning()
    return StubReasoning()


def create_app(reasoning: ReasoningPort | None = None) -> FastAPI:
    app = FastAPI(title="Steward Runtime", docs_url=None, redoc_url=None)
    port = reasoning or build_reasoning()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/invoke")
    def invoke(request: InvokeRequest) -> InvokeResponse:
        return port.invoke(request)

    return app


app = create_app()
