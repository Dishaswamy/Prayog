# SADD v2.0 conformance: the Steward inside the Prayog backend

How the merged code (2026-10-15) realises the architecture in `PRAYOG_SADD_v2.0.md`. Every **realised** row is exercised by a test or was observed live (see STATUS.md). **Deviation** rows say why and where the decision is recorded. **Not yet** rows belong to subsystems that are not built.

## Structure (SADD §3, containers, ADR-02)

| SADD element | Merged code | State |
|---|---|---|
| Modular monolith: one API code base, separate Worker, separate Steward Runtime | `backend/main.py` mounts the Steward (`integration/steward_bridge.py`). The Worker is two entry points: `logic.scheduler.run` (tick) and `integration.steward_worker` (Coordinator). The Runtime is `steward_runtime.app` on its own port | realised; observed live as 4 processes |
| PostgreSQL is the only correctness coordinator (ADR-03) | Steward tasks use `FOR UPDATE SKIP LOCKED` leases; the warning threshold takes a per-unit advisory lock; DB triggers enforce append-only records | realised |
| The Steward is a read-only black box behind a gateway (§3.5, ADR-18) | The Runtime has no database credentials and imports no server code (`lint-imports`). The server builds scoped, sanitised input and validates every response | realised |
| Steward reasoning uses the Anthropic Claude API | `ClaudeReasoning` (`claude-opus-5-5`, structured outputs). `heuristic`/`stub` for development and tests | realised; live Claude call not yet run (no key on the dev machine) |
| PostgreSQL 15+ **with pgvector** | Any PostgreSQL 15+. Embeddings are `real[]`, ranked in the application over RLS-filtered candidates | **deviation**: ADR-S09 |
| Content store for logs | `run_results.log_tail` (no API serves it; read only by the Steward through its sanitiser) | **stand-in** until the content store exists |
| Node agent reporting run ends | `scripts/simulate_agent.py` drives `lifecycle.release()` as the agent will | **not yet** (GPU sub-team) |

## Steward design (SADD §8)

| SADD element | Merged code | State |
|---|---|---|
| §8.1 One subsystem, four contexts; a failed rebuild fires S1 **and** S3 (FR-INT-01) | Rebuilds are ordinary jobs (`logic/rebuild.py`), so their release goes through the same seal hook (S1). Their attempt reaches REBUILD_FAILED and gets a link (S3) | realised (`test_failed_rebuild_opens_an_s3_session_and_success_allows_s4`) |
| §8.3 At seal of a FAILED run, **one transaction** inserts the failure event, fault evidence and Steward task | `lifecycle.release()` runs `seal_hook` before its commit, inside a savepoint | realised (`test_a_failed_run_is_sealed_for_the_steward_in_the_release`) |
| NFR-GPU-055 Scheduling never depends on the Steward | A failing hook rolls back only its savepoint; the release commits. With the Runtime down, runs still seal and jobs still schedule | realised (`test_a_broken_steward_never_blocks_a_release`; live outage test) |
| §8.2 Coordinator is the only writer; citations validated; refusals audited | `coordinator/service.py`, `coordinator/validation.py`; audit into the platform's `audit_events` | realised |
| §8.3 Admin sees the hardware finding and metadata only | Enum-only findings; ADMIN maps to Platform Admin, which has no route to narratives | realised (`test_s1_diagnosis_cites_the_log_and_respects_visibility`) |
| §8.3 Proposal approved through the ordinary submission path; commitment operation | `PlatformSubmissions` calls `submit_job()` with the proposal ID as `client_request_id` (FR-GPU-107 idempotency); refused once access has ended | realised (`test_approving_a_proposal_submits_a_real_job`, `test_approval_is_refused_after_access_expires`) |
| §8.4 Server threshold, advisory S2, admin-only close, evidence consumed, cooldown | `hardware.py`; admin routes on the platform's JWT | realised (`test_two_users_failing_on_one_gpu_raise_a_warning_for_the_admin`) |
| §8.5 Single-use 15-minute link stored hashed; guarded consume; identity checked before any context | `sessions/links.py` | realised; consumption moved from GET to POST, see below |
| §8.5 RLS on Steward tables with `app.user_id`; default-deny | Migration 0001. The Steward connects as `steward_app` (NOSUPERUSER NOBYPASSRLS), created by `scripts/setup_steward.py` | realised (`test_the_steward_role_cannot_bypass_row_level_security`) |
| §8.5 Sanitiser before RAG and search; output guard; retention purge | `rag/sanitiser.py`, S3 output guard, purge in the worker with the maintenance role | realised |
| §8.5 Professor boundary | Professors read supervised students' diagnoses (narrative only) and never S3 sessions | realised |

## Identity (SADD §5)

| SADD element | Merged code | State |
|---|---|---|
| Authorization decided server-side per request (ADR-A7) | `JwtPrincipalResolver` decodes the token and reads the user and their role from the database on every request | realised |
| Opaque server-stored session tokens; logout invalidates the session (ADR-A2, ID-12) | The platform issues 60-minute JWTs and has no server-side sessions. A Steward link is bound to the requesting token and dies when it expires | **platform deviation** (access sub-team); the Steward adapts in `PlatformAuthorizer.session_active` |
| Single append-only audit log (FR-INT-06) | `audit_events` with an UPDATE/DELETE-refusing trigger (migration `b7e4c1a9d2f0`) | realised (`test_steward_activity_lands_in_the_append_only_platform_audit_log`) |

## Deviations introduced by the merge, and why

1. **Link consumption is a POST** (`/steward/s/{token}/open`), not a GET. The platform authenticates with bearer tokens, which a browser cannot attach to a followed link. The link page therefore signs the user in through `/auth/login`, then consumes. As a side benefit, link previewers and prefetchers can no longer burn a single-use link.
2. **Projects are scoped to their owner** (`u<user id>:<project_ref>`), because there is no projects table yet. Two users who type the same `project_ref` never see each other's runs (FR-GPU-148). DEP-03 remains open for shared projects.
3. **Proposals change only GPU count and walltime.** Platform jobs carry no command, environment pins or batch size yet, so the model is told which fields are changeable (`settings.proposal_fields`), and an owner can never approve a change that would silently not happen.
4. **Rebuild attempts are implemented interim** in `backend/logic/rebuild.py` (ledger sub-team scope). Attempt state is derived from the job's state, so it cannot drift. A snapshot is accessible if one of the user's own jobs ran it.
