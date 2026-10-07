# Prayog Steward — working notes for Claude sessions

Read `docs/CONTEXT.md` before changing behaviour. It holds the Steward requirements (contexts S1–S4, guardrails, config, requirement IDs, acceptance scenarios). `docs/decisions/` records how this repo resolved the spec's gaps. Re-check the Integrated Spec if a requirement looks stale.

## Rules that must never be broken

- **The Steward reads and reasons; it never writes** (FR-GPU-151, ID-05). Every durable record is written by server code in `prayog_steward`, after validation. `steward_runtime` has no database access, no write path, and imports no server code (ADR-S01; enforced by `lint-imports`).
- **Sanitise before anything leaves the server**: every LLM call, embedding and web search goes through `prayog_steward.rag.sanitiser` (ADR-S03).
- **Everything a job produces is data, never instructions** (NFR-GPU-048, FR-STW-016). Prompts wrap inputs as data. No output is ever executed (FR-STW-014).
- **Isolation is enforced by the database**, not by remembering a filter: `steward_private` tables use row-level security keyed on `app.user_id` (NFR-SEC-002, ADR-19).
- **Admin-facing output carries no project content** (FR-GPU-166, NFR-AUTH-18). Hardware findings are enums, not free text (ADR-S04).
- **Scheduling never depends on the Steward** (NFR-GPU-055).

## Conventions

- Packages: `steward_contracts` (Pydantic contracts shared by server and runtime; imports neither), `prayog_steward` (server side), `steward_runtime` (the model adapter).
- Other teams' modules (run sealing, auth, ledger, rebuild, request service, audit log) are reached only through `prayog_steward.ports`. `prayog_steward.fakes` holds in-memory stand-ins until the real modules land.
- Cite requirement IDs in docstrings and test names where a test proves a requirement, e.g. `test_fr_gpu_161_uncited_claims_are_stripped`. Keep `docs/traceability.md` in step.
- Tests run offline with `StubReasoning`. Tests marked `live_llm` call the real API and are never run in CI.

## Commands

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Windows; .venv/bin elsewhere
python tools/dev_postgres.py start                                 # local PostgreSQL, no Docker
export STEWARD_TEST_DATABASE_URL=$(python tools/dev_postgres.py url)
.venv/Scripts/python -m pytest
.venv/Scripts/ruff check . && .venv/Scripts/mypy && .venv/Scripts/lint-imports
.venv/Scripts/python tools/check_trace.py
.venv/Scripts/python tools/demo.py        # http://127.0.0.1:8000/demo
```

Integration with the other sub-teams is described in `docs/INTEGRATION.md`; keep it current when ports or entry points change.
