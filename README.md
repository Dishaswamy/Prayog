# Prayog

**Prayog** is a shared GPU platform for IIT Kanpur. Students and researchers sign in with their IITK accounts and get access approved by their professor and an admin. They then submit GPU jobs to a fair-share scheduler that runs them on lab-owned GPU nodes. When a run fails, an AI agent called the **Steward** diagnoses the failure and suggests fixes.

> CS455 (Introduction to Software Engineering), IIT Kanpur, Group 3. This is an ongoing course project: see [Status](#status) for what works today and what is still in progress.

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Running the platform](#running-the-platform)
- [Trying it end to end](#trying-it-end-to-end)
- [Configuration](#configuration)
- [API overview](#api-overview)
- [Status](#status)
- [Further documentation](#further-documentation)

---

## What it does

| Area | What it does |
|---|---|
| **Authentication** | Self-registration for `@iitk.ac.in` students and researchers; JWT login (60-minute tokens); Argon2 password hashing. Professors are added by an admin; the first admin is created by a script. |
| **Authorisation** | Users request access, saying whether it is for a course project or for research (`purpose`). A request needs the approval of **both** their professor and an admin before it takes effect. Admins can revoke approved access, and a student's access ends automatically after their end date. If the user still has unfinished GPU jobs, the revoke waits for them to finish. Roles: `STUDENT`, `RESEARCHER`, `PROFESSOR`, `ADMIN`. |
| **GPU scheduling** | Jobs ask for a number of GPUs on one node and a walltime (`HH:MM`). A scheduler places them using **priority classes** (P0: your own lab's node, P1/P2: another lab's shared node, P3: teaching pool for course jobs), then a **fair-share score** inside each class (waiting time, recent usage with a 7-day half-life, lab contribution, job size). Retried submissions with the same `client_request_id` are idempotent. Course accounts have a GPU-hour quota. |
| **Node management** | Admins register nodes; each node's agent enrols with a one-time token, then heartbeats, pulls its allocations, and reports how each run ended. Nodes can be drained, returned, decommissioned, and booked for maintenance windows. A reaper marks silent nodes down and requeues their jobs. |
| **Steward (AI agent)** | **S1** diagnoses failed runs and may propose a corrected job (more GPUs or walltime) that the owner approves. **S2** warns admins when failures from several users point at the same GPU. **S3** gives a single-use chat link after a failed rebuild. **S4** gives optimisation advice after a successful rebuild, citing reference papers. The Steward only reads and reasons; the server validates and writes every record. |
| **Experiment Ledger (ML Flow)** | Projects, models, experiments and runs; metrics streamed during a run; content-addressed artifacts verified on upload; a snapshot sealed automatically at the end of a run (RECORDED, or INCOMPLETE with reasons); an outbox and idempotent ledger consumer; read-only Professor and admin views; IP handover to the supervising Professor; rebuilds from a Snapshot ID (`prayog run --rebuild`). See [backend/ML_FLOW.md](backend/ML_FLOW.md). |
| **Monitoring** | `GET /health` (API and database) and `GET /metrics` (scheduler counts in Prometheus format). |

## Architecture

Prayog runs as a few separate processes that share one PostgreSQL database:

```
                ┌──────────────────────────┐
  users ───────▶│  API  (FastAPI, :8000)   │◀────── node agents
  (JWT)         │  auth · access · jobs ·  │        (one per GPU node,
                │  admin · /steward/*      │         X-Node-Credential)
                └────────────┬─────────────┘
                             │
   ┌─────────────────────────┼───────────────────────────┐
   │                         ▼                           │
   │  Scheduler tick    PostgreSQL 16      Steward worker│
   │  (reaper + passes) platform tables +  (diagnoses,   │
   │                    steward schemas    correlation,  │
   │                    (row-level         retention)    │
   │                     security)              │        │
   └────────────────────────────────────────────┼────────┘
                                                ▼
                                  Steward Runtime (:8100)
                                  model adapter: Claude /
                                  heuristic / stub
                                  (no database access)
```

- **API** (`backend/main.py`): all HTTP endpoints. A scheduling pass also runs in the background after each job submission.
- **Scheduler tick** (`backend/logic/scheduler/run.py`): a safety-net pass every 10 seconds, plus the reaper for timeouts, stops and dead nodes.
- **Steward worker** (`backend/integration/steward_worker.py`): picks up Steward tasks, calls the Runtime, validates the answer and stores the diagnosis.
- **Steward Runtime** (`steward/src/steward_runtime`): the only part that talks to a model. It holds no database credentials, and import-linter rules forbid it from importing server code.
- **Node agent** (`backend/node_agent/simulated.py`): runs on each GPU node. The one in this repo uses a *simulated* executor, so the whole system can be run on a laptop without GPUs.

The Steward is optional. If it is disabled or not set up, the platform runs without it and scheduling is unaffected.

## Repository layout

```
Prayog/
├── docker-compose.yml          PostgreSQL 16 (+ Steward Runtime image)
├── .env.example                every setting, with comments; copy to .env
├── API.md                      API reference: health, auth, professors, access requests
├── API_STEWARD.md              API reference: rebuilds and /steward/*
├── backend/
│   ├── main.py                 FastAPI app, routers, /health
│   ├── database.py             SQLAlchemy engine from POSTGRES_* settings
│   ├── models.py               all platform tables
│   ├── alembic/                platform migrations (the only way tables are created)
│   ├── routers/                HTTP layer: auth, professors, access_requests, rebuilds,
│   │   └── scheduler/          jobs, admin (nodes, alerts, maintenance), agent, metrics
│   ├── schemas/                Pydantic request/response models
│   ├── logic/                  business rules; routers stay thin
│   │   ├── auth.py, access_request.py, admin.py, dependencies.py, errors.py, rebuild.py
│   │   └── scheduler/          classes, scoring, eligibility, passes, claim, dispatch,
│   │                           lifecycle, reaper, nodes, maintenance, metrics, config, run
│   ├── integration/            steward_bridge.py (the only module that knows both
│   │                           sides), steward_worker.py
│   ├── node_agent/             simulated node agent
│   ├── scripts/                create_admin, seed_dev, setup_steward
│   └── requirements.txt
└── steward/                    the Steward, an installable package (prayog-steward)
    ├── DESIGN.md               design, security model and decisions
    ├── pyproject.toml
    ├── Dockerfile
    ├── src/
    │   ├── steward_contracts/  Pydantic contracts shared by server and Runtime
    │   ├── prayog_steward/     server side: hooks, gateway, sanitiser, sessions, API, CLI
    │   └── steward_runtime/    model adapters: Claude, heuristic, stub
    ├── knowledge/              seed list of arXiv papers for the knowledge base
    ├── eval/                   S1 evaluation cases
    └── tools/evaluate.py       S1 accuracy evaluation
```

## Getting started

### Prerequisites

- Python **3.11+** (tested on 3.12 and 3.13)
- PostgreSQL **15+**. The easiest way is Docker, using `docker-compose.yml`.
- Git

### 1. Clone and install

```bash
git clone https://github.com/Dishaswamy/Prayog.git
cd Prayog
python -m venv .venv
```

Activate the environment and install the backend and the Steward package. The backend imports the Steward, so install both even if you plan to run with `STEWARD_ENABLED=false`.

```bash
# Linux / macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r backend/requirements.txt -e ./steward
```

### 2. Configure

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Then edit `.env`. At minimum, set:

- `POSTGRES_PASSWORD`
- `JWT_SECRET_KEY`, a long random string. For example: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `STEWARD_DB_PASSWORD`, which must differ from the Postgres password.

`.env` is git-ignored. Never commit it.

### 3. Start PostgreSQL

```bash
docker compose up -d postgres
```

### 4. Create the tables

All remaining commands run from `backend/`:

```bash
cd backend
python -m alembic upgrade head        # platform tables
python -m scripts.setup_steward       # Steward role, schemas and grants (skip if STEWARD_ENABLED=false)
```

`setup_steward` creates a separate, restricted database role for the Steward (`NOSUPERUSER NOBYPASSRLS`), so that row-level security on users' diagnostic data is enforced.

### 5. Create users

For a quick development setup with sample data:

```bash
python -m scripts.seed_dev --password dev-password
```

This creates `admin@iitk.ac.in`, `prof@iitk.ac.in`, `alice@iitk.ac.in` (student, lab *vision*) and `bob@iitk.ac.in` (researcher, lab *nlp*). Alice's and Bob's access is already approved. It also creates two nodes, `n1` and `n2`, with 4 GPUs each, and prints the command that starts each node's agent.

Or, to create just the first admin:

```bash
python -m scripts.create_admin admin@iitk.ac.in "StrongPassword123"
```

## Running the platform

Run each process in its own terminal, from `backend/`, with the virtual environment active:

```bash
uvicorn main:app --port 8000                                     # API  → http://127.0.0.1:8000/docs
python -m logic.scheduler.run                                    # scheduler tick
python -m integration.steward_worker                             # Steward worker
uvicorn steward_runtime.app:app --port 8100 --env-file ../.env   # Steward Runtime
```

The Runtime uses the rule-based `heuristic` adapter by default, so no API key is needed. To use Claude, set `STEWARD_REASONING=claude` and `ANTHROPIC_API_KEY` in `.env`.

Shortcuts for development:

- `STEWARD_INPROCESS_REASONING=heuristic` runs the model adapter inside the API, so you can skip starting the Runtime.
- `STEWARD_ENABLED=false` runs the platform without the Steward. You then only need the API and the scheduler tick.

Check that everything is up:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","database":"ok"}
```

## Trying it end to end

1. **Start a node agent.** Use the command printed by `seed_dev`, which looks like this:

   ```bash
   python -m node_agent.simulated --units 4 --state-dir .agent-n1 --enrolment-token <token>
   ```

   Its first heartbeat makes the node `AVAILABLE`. On later starts the stored credential is used, so the token isn't needed again.

2. **Log in and submit a job:**

   ```bash
   curl -X POST http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" \
        -d '{"email": "alice@iitk.ac.in", "password": "dev-password", "role": "STUDENT"}'

   curl -X POST http://127.0.0.1:8000/jobs -H "Authorization: Bearer <token>" \
        -H "Content-Type: application/json" \
        -d '{"gpus": 2, "walltime": "01:00", "client_request_id": "try-1"}'

   curl http://127.0.0.1:8000/jobs/<request_id> -H "Authorization: Bearer <token>"
   ```

   The job moves through `IN_PROCESS` → `QUEUED` → `SCHEDULED` → `RUNNING` → finished. A simulated job runs for 30 seconds by default (`--run-seconds`).

3. **See a Steward diagnosis.** Start an agent with `--exit-code 1 --log-file run.log`. Every job on that node then fails and reports the contents of `run.log` as its log. The Steward worker diagnoses the failure, and the owner can read it at `GET /steward/runs/{run_id}/diagnoses`.

The interactive API docs at `http://127.0.0.1:8000/docs` are the easiest way to try every endpoint.

## Configuration

All settings come from `.env`, and `.env.example` documents each one.

| Group | Variables |
|---|---|
| Database | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB` |
| Auth | `JWT_SECRET_KEY` |
| Steward | `STEWARD_ENABLED`, `STEWARD_DB_USER`, `STEWARD_DB_PASSWORD`, `STEWARD_RUNTIME_URL`, `STEWARD_PUBLIC_BASE_URL`, `STEWARD_SEARCH_PROVIDER` |
| Steward Runtime | `STEWARD_REASONING` (`claude` \| `heuristic` \| `stub`), `STEWARD_MODEL`, `ANTHROPIC_API_KEY` |
| Scheduler | Any field of `SchedulerConfig` in `backend/logic/scheduler/config.py`, as `PRAYOG_SCHED_<FIELD>`. For example, `PRAYOG_SCHED_MAX_WALLTIME_MINUTES=180`. |

The scheduler checks its configuration when it starts. An unknown variable, a non-numeric value, or a combination that breaks an invariant (for example, a heartbeat timeout shorter than three heartbeat intervals) stops startup with a message naming the problem.

## API overview

Every protected endpoint takes `Authorization: Bearer <token>` from `/auth/login`. Node-agent endpoints take `X-Node-Credential` instead. Every error response has a `detail` field.

| Area | Endpoints | Who |
|---|---|---|
| Health and metrics | `GET /health`, `GET /metrics` | anyone |
| Auth | `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | anyone / logged in |
| Professors | `GET /professors`, `POST /professors` | logged in / ADMIN |
| Access requests | `POST /access-requests`, `GET /access-requests/me`, `GET /access-requests`, `GET /access-requests/{id}`, `POST /access-requests/{id}/decision`, `POST /access-requests/{id}/revoke` | students and researchers request; professors and admins decide; admins revoke |
| Jobs | `POST /jobs`, `GET /jobs/quota`, `GET /jobs/{id}`, `PATCH /jobs/{id}`, `POST /jobs/{id}/cancel` | logged in |
| GPU pool (admin) | `/admin/nodes` (register, list, drain, return, decommission, escalate, unit count), `/admin/alerts`, `/admin/maintenance` (preview, book, change, cancel) | ADMIN |
| Node agent | `POST /agent/enrol`, `POST /agent/heartbeat`, `GET /agent/allocations`, `POST /agent/allocations/{id}/acknowledge`, `POST /agent/allocations/{id}/report` | node credential |
| Rebuilds | `POST /rebuilds`, `GET /rebuilds`, `GET /rebuilds/{id}` | logged in |
| Experiment Ledger | `/mlflow/*` (projects, models, experiments, runs, metrics, artifacts, snapshots, rebuilds, handover), `/mlflow/professor/*`, `/admin/mlflow/overview` | logged in / PROFESSOR / ADMIN; see [backend/ML_FLOW.md](backend/ML_FLOW.md) |
| Steward | `/steward/runs/…/diagnoses`, `/steward/proposals`, `/steward/attempts/…/link`, `/steward/s3/*`, `/steward/attempts/…/optimize`, `/steward/admin/*` | see [API_STEWARD.md](API_STEWARD.md) |

Full request and response formats are in [API.md](API.md) and [API_STEWARD.md](API_STEWARD.md).

## Status

**Working and tested.** Authentication, two-step access approval, the GPU scheduler (classes, fair-share scoring, dispatch, reaper, drain and maintenance), the simulated node agent, and the Steward contexts S1–S4 with the heuristic adapter. As of 2026-10-15, 16 end-to-end backend tests and 222 Steward tests pass on PostgreSQL. Those tests are kept outside this repository; the Experiment Ledger and scheduler-admin tests are in `backend/tests` and run in CI (`.github/workflows/mlflow-validation.yml`).

**Not built yet, or interim:**

- **Labs** have no API yet. `seed_dev` inserts them directly. A user's scheduling context is created when their access request is approved: `COURSE_PROJECT` gives a course account in the central lab, `RESEARCH_PROJECT` a research account in the supervising professor's lab (set `lab_id` when creating the professor).
- **Password reset emails.** The reset works, but without `SMTP_*` settings in `.env` the OTP is printed in the API terminal instead of being emailed.
- **Real node execution.** The node agent in this repo is simulated. It does not start containers or touch real GPUs.
- **Steward and ledger wiring.** The Experiment Ledger is in the repository, but the Steward still uses interim adapters: projects are scoped to their owner and `run_logs` holds the log tail.
- **Not yet verified.** A live Claude call, the Docker image, and the S1 accuracy evaluation on real, hand-labelled failed runs. `steward/eval/` currently holds a synthetic seed set.

## Further documentation

- [API.md](API.md): health, authentication and authorisation endpoints
- [API_STEWARD.md](API_STEWARD.md): rebuild and Steward endpoints
- [backend/ML_FLOW.md](backend/ML_FLOW.md): the Experiment Ledger lifecycle, artifacts, snapshots, rebuilds, the CLI and the isolated rebuild worker
- [backend/AI_INTEGRATION.md](backend/AI_INTEGRATION.md): the ledger outbox and the contract for Steward consumers
- [steward/DESIGN.md](steward/DESIGN.md): the Steward's design, security model, database layout, knowledge base and design decisions
