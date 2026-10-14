# ADR-S03 — Sanitise before every egress, including LLM calls

**Status:** Accepted (2026-10-07; decided with the subsystem owner) · **Closes:** CONTEXT §8 gap 2 · **Requirements:** FR-STW-011, 012, NFR-SEC-006, Integrated Spec §14.4 "Never"

## Context
§14.4 says the Steward never sends project code, weights, secrets or PII to an external service. FR-STW-012 only names RAG ingestion and web search. The Claude API is also an external service, and S1 reads log content.

## Decision
One server-side sanitiser (`prayog_steward.rag.sanitiser`) sits in front of **every** egress: LLM input for S1–S4, embeddings, and web-search queries.
- It removes secrets (API keys, tokens, passwords, private keys, connection strings), PII (emails, phone numbers, user home paths) and source code. Code is cut down to stack frames, keeping only file basename, line and function, never the source lines. Placeholders such as `[REDACTED:API_KEY]` keep the text readable.
- Web-search queries are not sanitised free text. They are **built** from an allow-listed structure: exception class, package names and versions (ADR-S06).
- Raw logs stay in the content store for authorised audit and are never passed to the pipeline (FR-STW-011).

## Consequences
- Diagnosis quality depends on what survives sanitisation. The Phase 7 evaluation must measure this.
- Redaction can miss novel secrets. The allow-listed search shape and the A23 corpus test are the backstops.
