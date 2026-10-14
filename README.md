# Prayog

Experiment ledger, Steward agent and fair GPU scheduling for campus ML research (CS455, Group 3).

| Part | Where | Owner |
|---|---|---|
| Backend framework, authentication, authorisation | `backend/` (`logic/auth.py`, `logic/access_request.py`, `routers/`) | access sub-team |
| GPU scheduling and execution | `backend/logic/scheduler/`, `backend/routers/scheduler/` | GPU sub-team |
| **Steward** (AI agent: run diagnosis, hardware warnings, rebuild help, optimisation) | `steward/` (package `prayog_steward`), wired in by `backend/integration/steward_bridge.py` | Steward |
| Rebuild attempts (interim, until the ledger lands) | `backend/logic/rebuild.py`, `backend/routers/rebuilds.py` | ledger sub-team |

API reference: [API.md](API.md) (auth and access requests), [steward/docs/API.md](steward/docs/API.md) (Steward and rebuilds), [steward/docs/INTEGRATION.md](steward/docs/INTEGRATION.md) (how the Steward is wired in), interactive docs at `/docs`.

## Setup

Requires Python 3.11+ and PostgreSQL 15+ (`docker compose up -d postgres`, or `python steward/tools/dev_postgres.py start` for a throwaway local cluster).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt -e "./steward[dev]"   # .venv/bin on Linux/macOS
cp .env.example .env                       # then set real secrets
cd backend
python -m alembic upgrade head             # platform tables
python -m scripts.setup_steward            # Steward role (no RLS bypass), schemas and grants
python -m scripts.create_admin admin@iitk.ac.in "<password>"
```

Optional:
- `python -m scripts.seed_dev` builds a development world: admin, professor, two approved users, two GPU nodes.
- Reference papers for the Steward: `pip install -e "./steward[knowledge]"`, then follow [INTEGRATION.md](steward/docs/INTEGRATION.md#f-knowledge-base-of-reference-papers-adr-s10).

## Running

Four processes, as in the architecture (SADD §3, ADR-02):

```bash
cd backend
uvicorn main:app --port 8000                          # API (+ Steward routes under /steward)
python -m logic.scheduler.run                         # scheduler safety tick
python -m integration.steward_worker                  # Steward worker: diagnoses, correlation, retention
uvicorn steward_runtime.app:app --port 8100 --env-file ../.env   # Steward Runtime (model adapter)
```

The Runtime uses Claude with `STEWARD_REASONING=claude` and `ANTHROPIC_API_KEY`. Without a key it uses `heuristic` (rule-based), which is also the default when the variable is unset; an unknown value stops the Runtime at startup. For a single process during development, set `STEWARD_INPROCESS_REASONING=heuristic` and skip the Runtime.

The node agent does not exist yet. `python -m scripts.simulate_agent fail <job_id> --log-file run.log` plays its part: it starts and ends a placed job through `lifecycle.release()`, exactly as the agent will.

## Tests

```bash
cd backend && pytest       # platform + Steward end to end on real PostgreSQL (PRAYOG_TEST_ADMIN_URL)
cd steward && pytest       # the Steward subsystem (STEWARD_TEST_DATABASE_URL)
```

Both run in CI (`.github/workflows/ci.yml`). Without a database URL the backend tests use the cluster from `steward/tools/dev_postgres.py`.

## How the Steward fits in

- **Run end:** `lifecycle.release()` writes the agent's report (`run_results`). It then runs the Steward's seal hook inside the release transaction, in a savepoint: a failed run's evidence and diagnosis task commit with the release, and a Steward fault never blocks it (NFR-GPU-055).
- **Diagnosis:** the Steward worker diagnoses through the Runtime. Owners read diagnoses at `/steward/runs/{allocation_id}/diagnoses` and approve corrected jobs at `/steward/proposals`; approval submits through `submit_job()`.
- **Hardware warnings:** failures from two or more users on one GPU raise a warning for admins at `/steward/admin/warnings`.
- **Rebuilds:** a failed rebuild (`POST /rebuilds`) gets a single-use Steward link (`POST /steward/attempts/{id}/link`) to an interactive diagnosis page. A successful one gets `POST /steward/attempts/{id}/optimize`.
- **Audit:** everything is recorded in `audit_events` (append-only).

Design documents: `steward/docs/` (requirements context, decisions ADR-S01…S10, SADD conformance, traceability, status).
