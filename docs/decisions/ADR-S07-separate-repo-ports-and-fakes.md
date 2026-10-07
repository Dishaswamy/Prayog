# ADR-S07 — Separate repo; other teams reached through ports with fakes

**Status:** Accepted (2026-10-07; decided with the subsystem owner)

## Context
The Steward depends on modules owned by other sub-teams: run sealing and the Results Service, authentication and authorization, the ledger and rebuild attempts, the Request Service, the audit log, and allocation history. Those may not exist yet.

## Decision
- `prayog_steward.ports` defines a `Protocol` for each dependency: `RunReader`, `LogStore`, `PrincipalResolver`, `Authorizer`, `RebuildAttempts`, `SubmissionPort`, `AuditSink`, `AllocationHistory`, `LedgerHistory`. `prayog_steward.fakes` provides in-memory implementations.
- Entry points the platform calls: `on_run_sealed(...)`, which writes the failure event, the evidence row and the Steward task **inside the platform's run-seal transaction** (SADD §8.3); FastAPI routers to mount; and a Typer sub-app for the CLI.
- Database: our tables live in our own schemas, `steward` and `steward_private`, in the shared PostgreSQL. Our Alembic version table is `steward.alembic_version`, so migrations never collide with the platform's.
- The SADD module layout (`prayog/steward/…`) maps one-to-one onto `prayog_steward/…`. Moving into a monorepo later is a rename.
