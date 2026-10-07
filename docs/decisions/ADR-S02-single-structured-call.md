# ADR-S02 — One structured call per invocation, no agent tool loop

**Status:** Accepted (2026-10-07) · **Requirements:** FR-GPU-146, 148, FR-STW-005, 013, NFR-INT-05

## Context
An agent loop with read tools would let the model choose what to read. Every context's inputs are already fully specified by the requirements:
- S1 reads the run, its telemetry, the sanitised log and the project's earlier runs (FR-GPU-148).
- S2 reads the bundle (FR-GPU-165).
- S3 reads the attempt's sanitised payload plus retrieved ledger evidence (FR-STW-005, 015).
- S4 reads the run's metrics plus the user's history (FR-STW-006).

## Decision
Each invocation is a **single** Claude Messages API call through `client.messages.parse()` with a Pydantic `output_format` (structured outputs). S3 is a chat: each user turn is one call, and the server re-retrieves context for that turn.

## Consequences
- Deterministic, cheap, and fully testable with `StubReasoning`.
- If evaluation shows the model needs to pull more evidence, a read-only tool loop can be added behind the same `ReasoningPort`, with tools still served by the Gateway. Doing so needs a new ADR.
