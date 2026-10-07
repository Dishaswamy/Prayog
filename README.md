# prayog-steward

The **Steward**, Prayog's AI agent (CS455 Group 3). One subsystem, four invocation contexts (Integrated Spec ID-05):

| Context | Trigger | Output |
|---|---|---|
| **S1** Run-failure diagnosis | A run ends `FAILED` | Cited diagnosis for the owner, a separate hardware finding for the admin, and an optional corrected job that needs the owner's approval |
| **S2** Fault correlation | The server sees failures from ≥ 2 users on one unit within 24 h | Advisory judgement attached to a hardware warning |
| **S3** Rebuild diagnosis | A `--rebuild` attempt reaches `REBUILD_FAILED` | Interactive diagnosis over a user-isolated RAG pipeline, opened by a single-use link |
| **S4** Optimisation advisory | `prayog steward optimize` on a `REBUILD_SUCCESS` attempt | Advisory architecture and hyperparameter suggestions |

Governing principle: **the Steward reads and reasons; it never writes.** The server writes every record.

## Layout

```
src/steward_contracts/   Pydantic contracts shared by server and runtime (inputs, results, claims)
src/prayog_steward/      server side: coordinator, gateway, evidence, warnings, sessions, rag, search, api, db
src/steward_runtime/     the reasoning adapter (ClaudeReasoning, StubReasoning); no DB access
docs/                    CONTEXT.md (requirements), decisions/ (ADR-S01…), traceability.md
tests/                   unit/ integration/ security/ acceptance/ eval/
```

## Getting started

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # .venv/bin/pip on Linux/macOS
.venv/Scripts/python -m pytest            # offline, uses StubReasoning
```

Tests marked `postgres` need PostgreSQL 15 + pgvector. Start it with `docker compose up -d db` and set `STEWARD_TEST_DATABASE_URL`. CI runs them against a service container.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Bootstrap: docs, tooling, CI | done |
| 1 | Contracts, ports + fakes, reasoning port, DB schema | done (Postgres tests run in CI) |
| 2 | S1 end to end | — |
| 3 | S2 and hardware warnings | — |
| 4 | Sanitiser, RAG foundation, links | — |
| 5 | S3 interactive diagnosis and web UI | — |
| 6 | S4 optimize | — |
| 7 | Hardening, retention, evaluation | — |
