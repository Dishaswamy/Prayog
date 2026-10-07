# prayog-steward

The **Steward**, Prayog's AI agent (CS455 Group 3). One subsystem with four invocation contexts (Integrated Spec ID-05), all governed by one rule: **the Steward reads and reasons; it never writes.** The server writes every record, after validating what the model said.

| Context | Trigger | What happens |
|---|---|---|
| **S1** Run-failure diagnosis | A run is sealed `FAILED` | The Worker builds sanitised, scoped input and calls the model. It stores a diagnosis whose uncited claims are stripped, an enum-only hardware finding for the admin, and optionally a bounded corrected-job proposal. The owner sees the exact diff and GPU-hours and approves through ordinary submission. |
| **S2** Fault correlation | ≥ 2 users fail on one GPU within 24 h | The **server** raises the warning deterministically. The model's correlation over a metadata-only bundle is attached as advice. Only an admin closes it, with a reason. |
| **S3** Rebuild diagnosis | A `--rebuild` attempt ends `REBUILD_FAILED` | The CLI prints a single-use, 15-minute link. The browser session's answers come from the user's own records only (row-level security, partitioned by attempt), plus allow-listed web search. Steps are text only, never executed. |
| **S4** Optimisation advisory | `prayog steward optimize` on `REBUILD_SUCCESS` | Advisory suggestions from the run's metrics and the user's own history. |

## Try it in two minutes

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # .venv/bin on Linux/macOS
python tools/dev_postgres.py start      # throwaway PostgreSQL from your installed binaries
python tools/demo.py                    # rule-based reasoning; add --claude for the real model
```

Open <http://127.0.0.1:8000/demo>. It runs the live-demo scenarios: a failed run diagnosed with an approvable fix, a hardware warning from two labs, and a failed rebuild opening the interactive session. With Docker: `docker compose up --build`.

## Layout

```
src/steward_contracts/    Pydantic contracts shared by server and runtime (leaf package)
src/prayog_steward/       server side, the only writer
  hooks.py                on_run_sealed: called inside the platform's seal transaction
  coordinator/            task outbox, S1/S2 processing, validation, bounded proposals
  gateway.py              scoped, sanitised model input (the only path from data to model)
  hardware.py             warning threshold, lifecycle, admin view
  owner.py                diagnoses and proposal approval
  sessions/               S3 links and sessions, isolated RAG context, S3 chat, S4 optimize
  rag/                    sanitiser, embeddings, chunking
  api/                    FastAPI router, standalone app, static S3 UI
  cli.py                  `prayog steward ...` Typer sub-app
  retention.py            purge on deactivation and after the retention period
  db/                     tables and Alembic migrations (RLS, append-only triggers)
src/steward_runtime/      the model adapters: ClaudeReasoning, HeuristicReasoning, StubReasoning
docs/                     CONTEXT.md, INTEGRATION.md, decisions/ADR-S01…S09, traceability.md
tools/                    dev_postgres.py, demo.py, evaluate.py, check_trace.py
eval/                     S1 evaluation cases (seed set is synthetic)
tests/                    unit/ (offline) and integration/ (real PostgreSQL, real RLS)
```

## Development

```bash
python tools/dev_postgres.py start
export STEWARD_TEST_DATABASE_URL=$(python tools/dev_postgres.py url)
python -m pytest                    # without the variable, database tests are skipped
ruff check . && mypy && lint-imports && python tools/check_trace.py
python tools/evaluate.py [--claude]
```

CI runs all of the above against PostgreSQL 15 on every push and PR.

## Documents

- [docs/INTEGRATION.md](docs/INTEGRATION.md): what the other sub-teams implement and call. **Start here to merge.**
- [docs/CONTEXT.md](docs/CONTEXT.md): the requirements this subsystem implements.
- [docs/decisions/](docs/decisions/): how the spec's gaps were resolved (ADR-S01…S09).
- [docs/traceability.md](docs/traceability.md): requirement → code → test.
