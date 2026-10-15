# ADR-S01 — The Runtime has no data access; the server assembles scoped input

**Status:** Accepted (2026-10-07) · **Closes:** CONTEXT §8 gaps 1 and 6 · **Requirements:** FR-GPU-148, 151, FR-AUTH-73, NFR-SEC-002, SADD ADR-18

## Context
SADD §8.2 gives the Steward Runtime a `SELECT`-only database role and has it read through a Tool Gateway. SD-17, however, has the Runtime "sanitise then ingest" into the vector store, and ingestion is a write. The Project Document also says tool calls run "with the invoking person's permissions", which SADD ADR-18 narrows to a read-only scoped token.

## Decision
- `steward_runtime` holds **no database credentials at all** and imports no server code. It exposes a single operation: `invoke(request) -> response`.
- The server-side **Gateway** (`prayog_steward.gateway`) mints a short-lived scoped token (context, run or attempt ID, owner) and assembles the input from it. The predicates come from the token, never from the model. Any read outside the scope is refused and audited.
- Sanitising, embedding and writing `RAG_CHUNK`, caches and prompt history happen **server-side** in the Session Service. Retrieval also runs server-side, under row-level security.
- The token scope is the run owner's (S1) or attempt owner's (S3/S4) reach, narrowed to read-only. S2 runs with no user context.

## Consequences
- An injected or hallucinating model has no read or write path to abuse. Its only channel is a schema-validated response.
- The "no write" property is checkable statically (`lint-imports` forbids DB libraries in `steward_runtime`) and at runtime (response contracts forbid extra fields; ADR-S04).
- Cost: the server must decide up front what context each invocation needs (ADR-S02).
