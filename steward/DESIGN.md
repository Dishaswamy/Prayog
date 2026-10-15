# Steward: design and operation

The **Steward** is Prayog's AI agent (Integrated Spec ID-05; SADD §8). It is one subsystem with four invocation contexts, and one rule governs it: **the Steward reads and reasons; it never writes.** The server writes every record, after validating what the model said.

Endpoints are documented in [API_STEWARD.md](../API_STEWARD.md). This file covers what the Steward does, how it is wired into the backend, how to run it, and the decisions behind it.

## Contents

1. [What it does](#1-what-it-does)
2. [Setup and running](#2-setup-and-running)
3. [How it is wired in](#3-how-it-is-wired-in)
4. [Database](#4-database)
5. [Security model](#5-security-model)
6. [Knowledge base of reference papers](#6-knowledge-base-of-reference-papers)
7. [Configuration](#7-configuration)
8. [Hooks for other sub-teams](#8-hooks-for-other-sub-teams)
9. [Decisions](#9-decisions)
10. [Deviations from the SADD](#10-deviations-from-the-sadd)
11. [Status and open items](#11-status-and-open-items)

---

## 1. What it does

| Context | Trigger | What happens |
|---|---|---|
| **S1** Run-failure diagnosis | A run ends `FAILED` | The Worker builds sanitised, scoped input and calls the model. It stores:<br>• a diagnosis whose uncited claims are stripped;<br>• an enum-only hardware finding for the admin;<br>• optionally, a bounded corrected-job proposal. The owner sees the exact change and its GPU-hours, and approves it through ordinary submission. |
| **S2** Fault correlation | Failures from ≥ 2 users on one GPU within 24 h | The **server** raises the warning deterministically. The model's correlation over a metadata-only bundle is attached as advice. Only an admin closes the warning, with a reason. |
| **S3** Rebuild diagnosis | A rebuild ends `REBUILD_FAILED` | The user gets a single-use, 15-minute link to a chat page. Answers come from the user's own records only, under row-level security per attempt, plus optional allow-listed web search. Steps are text only, never executed. |
| **S4** Optimisation advice | `optimize` on a `REBUILD_SUCCESS` attempt | Advisory suggestions from the run and the user's own history, citing published papers where they apply. |

S3 and S4 also draw on a shared **knowledge base of reference papers** (§6). Claims that rest on it are labelled *literature* and link the arXiv paper.

**Code layout** (`steward/src/`):

| Package | Contents |
|---|---|
| `steward_contracts` | Pydantic contracts shared by server and Runtime |
| `prayog_steward` | The server side, the only writer:<br>• `hooks.py`: `on_run_sealed`<br>• `coordinator/`: task outbox, validation, proposals<br>• `gateway.py`: scoped, sanitised model input<br>• `hardware.py`: warnings<br>• `owner.py`: diagnoses and approvals<br>• `sessions/`: S3 links and chat, S4<br>• `rag/`: sanitiser and embeddings<br>• `knowledge/`: paper corpus<br>• `api/`: routes and the S3 page<br>• `cli.py`: `prayog steward ...`<br>• `retention.py`: purge<br>• `db/`: tables and migrations |
| `steward_runtime` | The model adapters: `ClaudeReasoning` (`claude-opus-5-5`), `HeuristicReasoning` (rule-based, no API key), `StubReasoning` |

## 2. Setup and running

Requires Python 3.11+ and PostgreSQL 15+ (no extensions).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt -e ./steward   # .venv/bin on Linux/macOS
cp .env.example .env                         # set STEWARD_DB_PASSWORD and the other secrets
cd backend
python -m alembic upgrade head               # platform tables, including run_logs, rebuild_attempts, audit_events
python -m scripts.setup_steward              # the Steward's database role, its schemas and grants (§4)
```

Processes (SADD §3, ADR-02), all from `backend/`:

```bash
uvicorn main:app --port 8000                                     # API, with the Steward under /steward
python -m logic.scheduler.run                                    # scheduler tick (reaper + passes)
python -m integration.steward_worker                             # Steward worker: diagnoses, correlation, retention purge
uvicorn steward_runtime.app:app --port 8100 --env-file ../.env   # Steward Runtime (the model adapter)
```

- **Model adapter:**
  - `STEWARD_REASONING=claude` with `ANTHROPIC_API_KEY` uses Claude.
  - Without a key, use `heuristic`, which is also the default.
  - An unknown value stops the Runtime at startup.
  - For a single process during development, set `STEWARD_INPROCESS_REASONING=heuristic` and skip the Runtime.
- **Without the Steward:** `STEWARD_ENABLED=false` runs the platform without it. If its database role is missing, the platform also starts without it and logs a warning.
- **Development world:** `python -m scripts.seed_dev --password <pw>`.
  - It creates an admin, a professor and two approved users (alice in lab vision, bob in lab nlp).
  - It registers nodes n1 and n2, and prints the command that starts each node's simulated agent.
  - To see a diagnosis, start an agent with `--exit-code 1 --log-file run.log`: every job it runs then fails and reports that log.

## 3. How it is wired in

`backend/integration/steward_bridge.py` is the only module that knows both sides. It implements the Steward's nine ports against the platform's tables, and registers the run-seal hook.

| Concern | Where |
|---|---|
| The nine ports: runs, logs, principals, authorizer, rebuild attempts, submissions, audit, allocation history, ledger history | `integration/steward_bridge.py` |
| Run-seal hook inside the release transaction | `logic/scheduler/lifecycle.py` (`register_release_hook`) |
| Log tail from the agent's report | `ReportIn.log_tail` → `dispatch.report()` → `release(log_tail=...)` → `run_logs` |
| API and the S3 page | `main.py` → `install_steward(app)` mounts everything under `/steward` |
| Steward worker | `python -m integration.steward_worker` |
| Rebuild attempts (interim, ledger scope) | `logic/rebuild.py`, `routers/rebuilds.py` |

**A run end, step by step:**
1. The node agent reports the exit (`POST /agent/allocations/{id}/report`, optionally with `log_tail`). Node failures come instead from the reaper, and cancels from `/jobs/{id}/cancel`.
2. All of these go through `lifecycle.release()`. It closes the `runs` row and stores the log tail in `run_logs`.
3. Before the commit, it runs the Steward's seal hook inside a savepoint. For a `FAILED` run, the hook writes the failure event, per-GPU fault evidence and an S1 task. For a node failure it writes evidence only. A Steward error rolls back only its savepoint: **the release always commits** (NFR-GPU-055).
4. The Steward worker claims the task, has the Runtime reason over sanitised input, validates the answer and stores the diagnosis.

**Mapping decisions:**
- **Run:** a row of `runs` (one execution of a job); `run_id` is `runs.id`.
- **Project:** `u<owner id>:<project_ref>`. There is no projects table yet, so a project is scoped to its owner.
- **Principal:** the JWT's user. A token is valid exactly when the platform's `get_current_user` accepts it, so any revocation rule added there also covers `/steward`. ADMIN maps to Platform Admin.
- **Job spec:** GPUs, walltime and snapshot. Proposals may change only `gpu_count` and `walltime_minutes`.
- **Audit:** the platform's append-only `audit_events`.

## 4. Database

The Steward uses **the same database** as the platform. It does not create a new one.

- **Platform tables**, through the platform's own Alembic history (migration `c3a8d5e1f7b2`):
  - `run_logs`: the end of a run's output, a stand-in for the content store;
  - `rebuild_attempts`: interim;
  - `audit_events`: append-only, enforced by a trigger.
- **Steward tables** live in two schemas of that database, `steward` and `steward_private`. They have their own Alembic history (version table `steward.alembic_version`), so the two never collide. `scripts/setup_steward.py` applies it.
- **Its own role:** `setup_steward` creates the role the Steward connects as (`STEWARD_DB_USER`, `NOSUPERUSER NOBYPASSRLS`).
  - This is what enforces row-level security on `steward_private`, where each row belongs to one user. One user's diagnostic data can never be read by another, even through a bug (NFR-SEC-002, SADD §8.5).
  - A superuser or a `BYPASSRLS` role would skip those policies, so `setup_steward` refuses to finish if the role has either.
- **Maintenance work:** the retention purge and admin health counts use the platform's owner connection.
- Embeddings are stored as `real[]` and ranked in Python (ADR-S09), so pgvector is not needed.

## 5. Security model

- **Sanitiser before every egress** (ADR-S03): every model input, embedding and web-search query is sanitised first. It removes secrets, personal data and source code. Raw logs never leave the server.
- **The Runtime has no data access** (ADR-S01). It holds no database credentials and imports no server code. The server assembles scoped input and validates every output.
- **Citations are validated** (ADR-S04). Claims that cite nothing, or cite records outside their scope, are dropped. Hardware findings are enums only, so an admin never sees a user's narrative.
- **Visibility:**
  - Anything you may not see answers 404.
  - Admin routes answer 404 to non-admins.
  - Professors read their supervised users' diagnoses, but never S3 sessions.
- **S3 links:**
  - Each link is single-use, valid for 15 minutes and stored hashed.
  - The page has no side effects; the link is consumed by POST.
  - Identity is checked before any context is shown, and the session cookie is HttpOnly.
- **CSRF:** cookie-authenticated mutations need the `X-Steward-CSRF: 1` header.
- **Retention:** data is purged after 90 days and on account deactivation (`purge_user`).
- **Never blocks scheduling:** with the Runtime down, runs still seal, tasks wait and jobs keep scheduling.

## 6. Knowledge base of reference papers

S3 and S4 cite published papers from a shared corpus (ADR-S10, ported from the team's Scientific-RAG-System). Build it once per environment, as the database owner (the Steward's role is read-only on it). From `steward/`:

```bash
pip install -e ".[knowledge]"                                   # PyMuPDF + BGE embeddings
python -m prayog_steward.knowledge fetch --ids-file knowledge/seed_arxiv.txt
STEWARD_DATABASE_URL=<owner url> python -m prayog_steward.knowledge ingest
python -m prayog_steward.knowledge search "loss diverges with a large batch"
```

- **Adding papers:** by arXiv ID (`fetch --ids ...`), by topic (`fetch --topic ... --max N`) or as a folder of PDFs (`ingest --pdf-dir`). Re-ingesting is idempotent.
- **Seed corpus:** 15 papers chosen for what the Steward diagnoses (299 chunks). Relevance is calibrated on BGE: the minimum score is 0.58.
- **Model download:** the first use downloads the BGE model (~440 MB). If that fails with "CAS Client Error", set `HF_HUB_DISABLE_XET=1`. Once it is cached, `HF_HUB_OFFLINE=1` skips the Hub checks.
- **Cold start:** the API loads the model in the background at startup. Requests during that window wait for the single load.

## 7. Configuration

Every setting is an environment variable prefixed `STEWARD_`; see `.env.example` and `src/prayog_steward/config.py`. The main ones:

| Variable | Meaning |
|---|---|
| `STEWARD_DB_USER`, `STEWARD_DB_PASSWORD` | the Steward's database role (§4) |
| `STEWARD_RUNTIME_URL` | where the Runtime listens (default `http://localhost:8100`) |
| `STEWARD_PUBLIC_BASE_URL` | the base of S3 links |
| `STEWARD_ENABLED` | `false` runs the platform without the Steward |
| `STEWARD_SEARCH_PROVIDER` | `none` (default), `tavily` or `brave`, plus `STEWARD_SEARCH_API_KEY` |
| `STEWARD_REASONING`, `STEWARD_MODEL`, `ANTHROPIC_API_KEY` | the Runtime's model adapter |

The spec's parameters have the spec's defaults:

| Parameter | Default |
|---|---|
| Link lifetime | 15 min |
| Correlation window | 24 h |
| Distinct users for a warning | 2 |
| Warning cooldown | 24 h |
| Evidence bundle | ≤ 50 rows |
| Retention | 90 days |
| Earlier runs considered | 10 |

## 8. Hooks for other sub-teams

- **CLI:** mount `prayog_steward.cli.app` as `prayog steward` (`link`, `optimize`, `diagnosis`, `proposals`, `approve`, `reject`). After a `REBUILD_FAILED` attempt, print `report_rebuild_failure(...)`: the attempt ID plus the Steward link.
- **Account deactivation (Access):** call `prayog_steward.retention.purge_user(engine, user_id)`. It removes personal Steward state; diagnoses and projects stay.
- **Any process that releases runs:** either `install_steward(app)` (the API) or `register_steward_hooks()` (the scheduler tick already does this). Otherwise the Steward misses those run ends.
- **Content store, ledger, projects table, server-side sessions:** when each lands, it replaces the matching adapter in the bridge. Nothing inside `steward/` changes.

## 9. Decisions

Code comments cite these IDs.

| ID | Decision |
|---|---|
| ADR-S01 | The Runtime has no data access. The server assembles scoped input from a short-lived scope and refuses and audits anything outside it. |
| ADR-S02 | One structured model call per invocation (S3: one per chat turn). No agent tool loop. |
| ADR-S03 | One sanitiser in front of every egress: model input, embeddings, search. Search queries are built from an allow-listed structure (exception class, packages), never from free text. |
| ADR-S04 | Strict output contracts (`extra="forbid"`, so a "write-attempting" response is rejected and audited). Citation validation. Enum-only hardware findings. The server computes a proposal's diff and GPU-hours. |
| ADR-S05 | S2 is advisory: the server raises warnings. Evidence rows may carry a sanitised `failure_signature` (change request CR-S1). |
| ADR-S06 | Local embeddings (no data leaves the server). Web search through an allow-listed adapter, not Claude's own search tool. |
| ADR-S07 | Other sub-teams are reached through nine ports. The Steward's tables live in its own schemas and migrations. |
| ADR-S08 | Placeholder defaults for open items (§11). |
| ADR-S09 | No pgvector: embeddings are `real[]`, ranked in Python over RLS-filtered candidates. |
| ADR-S10 | A shared knowledge base of papers for S3 and S4, in PostgreSQL. Generation stays on Claude; Gemini, LangChain and Qdrant from the notebooks are not adopted. |

## 10. Deviations from the SADD

1. **Link consumption is a POST** (`/steward/s/{token}/open`), not a GET. Bearer tokens cannot ride on a followed link, so the page signs the user in first. Link previewers also cannot burn a single-use link.
2. **Projects are scoped to their owner** until a projects table exists. DEP-03 (shared projects) stays open.
3. **Proposals change only GPU count and walltime**, because platform jobs carry no command, environment or batch size yet. The rule-based adapter's only proposal (halving the batch size) therefore never applies, and live proposals come only from Claude.
4. **Rebuild attempts are interim** (ledger scope). An attempt's state is derived from its job, so it cannot drift.
5. **Sessions:** the platform issues 60-minute JWTs with no server-side sessions. A Steward link dies when the token that requested it expires.
6. **Logs:** `run_logs` holds the agent-reported tail until the content store exists. No API serves it.

## 11. Status and open items

- **Verified on 2026-10-15** against this code:
  - 16 end-to-end backend tests and 222 Steward tests passed on PostgreSQL, under Python 3.12 and 3.13. At the team's request, the tests are kept outside this repository.
  - A live run with every process and two simulated node agents:
    - A failed run was diagnosed, citing its log, with a planted API key removed.
    - A killed agent let the reaper mark its node down. The Steward recorded evidence without a diagnosis, and the job was requeued.
    - Another user got 404 on that run.
- **Not yet verified:**
  - a live Claude call (no API key on the development machine);
  - the Docker image;
  - the real accuracy evaluation. `python tools/evaluate.py [--claude]` runs it over `eval/s1_cases.jsonl`, which is a synthetic seed set; the Project Document asks for ~100 real, hand-labelled failed runs.
- **Open items with the defaults in use (ADR-S08):**
  - S1 failure classes: `oom`, `missing_data`, `library_version`, `code_bug`, `interruption`, `hardware`, `unknown`.
  - The metrics S4 may use.
  - Whether S3 shows the S1 diagnosis.
  - Calibration of the correlation window.
- **To confirm with other sub-teams:**
  - CR-S1, the `failure_signature` field.
  - The platform cookie name (`prayog_session`) and the `X-Steward-CSRF` convention.
  - Revoking tokens on password reset. API.md promises it, but it is not in the code yet; once added to `get_current_user`, it covers the Steward automatically.
