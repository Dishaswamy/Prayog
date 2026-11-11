---
title: "Prayog — Requirements → Jira → GitHub → Testing Traceability Matrix"
subtitle: "CS455 Deliverable 3 · Group 3 · 6 November 2026"
---

# 1. How the matrix is built

The chain is the one in the sprint plan section 12: **requirement ID** (Integrated Requirements Specification) → **Jira issue** (story or task) → **GitHub** (pull request or commit) → **code** → **automated test** → **result on 6 November**. Every column is generated, not typed, by `src/make_trace.py`:

| Column | Source |
|---|---|
| Requirement(s) | requirement IDs (`FR-…`, `NFR-…`) in the backlog item's description (`PRAYOG_backlog_jira_import.csv`) |
| Jira, Status | the Jira export (Jira key = plan key + 4) |
| Code | source files on `main` that cite the requirement ID in a comment or docstring (189 distinct IDs are cited in code) |
| GitHub | merged pull requests whose branch names the Jira key (`#n` = `Dishaswamy/Prayog`; `wr#n` = the working repository `baymaxisme/Prayog`, which used plan keys and is converted), plus PRs mapped by content (#11 Steward, #12 ledger, #19 access purpose, #21/#22 scheduler, #23–#25 CLI, #26 web app); otherwise commits whose message names the key |
| Automated tests | test modules that exercise the story, from reading the tests (`src/trace_map.py`) |
| Result | pass/fail counts of those modules in the 6 November junit files |

# 1.1 The critical requirements, end to end

For the course's 8–10 critical requirements, the full chain. All results are from the final Linux run (`evidence/junit_backend_linux.xml`).

| Requirement | Jira | PR | Test that proves it | Module (tests passing) | Result |
|---|---|---|---|---|---|
| NFR-GPU-001, FR-GPU-055: one live holder per GPU unit | PRAYOG-53 | #5, #21 | `test_passes_racing_for_the_last_free_unit_allocate_it_once` | `test_claim` (27) + `test_concurrency` (6) | pass |
| FR-GPU-054: all-or-nothing multi-GPU claim | PRAYOG-53 | #5 | `test_four_requested_with_only_three_lockable_holds_nothing` | `test_claim` (27) | pass |
| FR-GPU-013 to 016: rejection before queueing | PRAYOG-47 | #6 | `test_impossible_request_is_rejected_on_the_spot_and_stores_nothing` | `test_requests` (60) + `test_jobs_api` (29) | pass |
| FR-GPU-044 to 049, 185: score formulas and rounding | PRAYOG-51 | #4 | `test_derived_bounds_match_the_specification` | `test_scoring` (28) | pass |
| NFR-GPU-009, 060: starvation bounds | PRAYOG-105 | `d3-final` | `test_research_job_waiting_90_minutes_longer_always_ranks_first` | `test_scoring` (28) | pass |
| FR-GPU-060, 061: idempotent dispatch | PRAYOG-59 | #9 | `test_acknowledging_starts_the_run_once` | `test_dispatch` (20) | pass |
| FR-GPU-065 to 069: cancel with two-phase release | PRAYOG-63 | #9 | `test_cancelling_a_running_job_frees_its_gpus_only_after_the_node_confirms` | `test_dispatch` (20) + `test_agent_api` (18) | pass |
| FR-GPU-186: owner-claim guard | PRAYOG-53 | #5 | `test_guest_claim_rolls_back_when_the_owner_has_an_eligible_job` | `test_claim` (27) | pass |
| FR-GPU-077, 135, 136: node failure, and an outage is not one | PRAYOG-64 | `d3-final` | `test_an_api_outage_longer_than_the_timeout_marks_no_node_down` | `test_reaper` (17) + `test_liveness` (9) | pass |
| NFR-GPU-030, A-18: another user's job | PRAYOG-108 | `d3-final` | `test_another_users_job_cannot_be_read_edited_or_cancelled` | `test_security` (25) | pass |

# 2. Coverage of the chain

| Link in the chain | Items | Share |
|---|---|---|
| Backlog items (77 stories, 26 tasks; epics excluded) | 103 | — |
| Done in the Jira export | 87 | 84% |
| Carry at least one requirement ID | 83 | 81% |
| Requirement cited in code on `main` | 50 | 49% |
| Linked to a merged pull request or a commit naming the key | 61 | 59% |
| Mapped to automated tests in the repository | 84 | 82% |
| … of which every mapped test passes on 6 Nov | 67 | |
| … with at least one test that fails on Windows only (`os.killpg`, symlinks, permission bits, path separators; all pass on Linux) | 8 (PRAYOG-30, -44, -45, -60, -61, -71, -72, -94) | |
| No automated test, but other evidence (CI steps, drills, probes, D3 documents) | 18 | |
| No evidence at all | 6 Done items: PRAYOG-21, -38, -40, -41, -43, -69 | |

# 3. Gaps

**Done in Jira but without an automated test in the repository:** PRAYOG-17, -18, -19, -20, -21, -22, -23, -24, -38, -40, -41, -43, -56, -57, -68, -69, -74, -112 (18 items).

Most of these are platform tasks whose evidence is the CI workflow or a document rather than a test (PRAYOG-17…24). The functional gaps are PRAYOG-38 (access extension), -40 (one person holding admin and submitting rights), -41 (account deactivation), -43 (professor continuing access), -56 (show remaining cap), -57 (edit a queued job), -68 (tell the owner no node can ever run the job), -69 (CLI notifications) and -74 (node-down alert, exercised only by the live drills). They are the first items of the Sprint 3 test backlog (Project report section 6).

**Steward stories (PRAYOG-95…102)** are covered by the 222-test Steward suite, which is kept outside `main` at the team lead's request (branch `steward-full`), and by the 17-test backend–Steward harness; neither runs in CI on `main`.

**Requirement coverage.** The Integrated Specification defines 400 requirement IDs (SADD Appendix A traces all of them to design). On `main`, 189 are cited in code and 35 in tests; most tests are named by behaviour rather than by requirement ID, which is why the story-level matrix below maps tests by reading them.

# 4. D3 defects → fix → regression test

| Defect | Requirement | Jira (after import) | Fix | Regression test | Result |
|---|---|---|---|---|---|
| D3-01 | FR-STW-001, FR-STW-006, FR-INT-01 | Bug → PRAYOG-14 | `8a38f79` (PR #26) | `test_cli_rebuild_reaches_steward` (2) | pass |
| D3-02 | FR-AUTH-60, FR-AUD-01 | Bug → PRAYOG-6 | `717f1fc` (PR #26) | `test_review_fixes` lockout (4), concurrency C4, probe A19 | pass |
| D3-03 | FR-GPU-055, FR-GPU-056 | Bug → PRAYOG-8 | `f358e01` | concurrency C6 | pass (fails on `main`) |
| D3-04 | FR-GPU-154 | Bug → PRAYOG-9 | `f358e01` | `test_job_list_batched` (3) | pass |
| D3-05 | NFR-SEC (node isolation) | Bug → PRAYOG-9 | `f358e01` | `test_node_agent_units` (POSIX) | pass in CI |
| D3-06 | availability | Bug → PRAYOG-5 | `f358e01` | `test_database_unavailable` (2), drill F3 | pass |
| D3-07 | performance | Bug → PRAYOG-5 | `f358e01` | load re-run | 0% errors |
| D3-08 | FR-GPU-077 | Bug → PRAYOG-11 | `d3-final` | `test_liveness` (9), failure experiment | pass |
| D3-09 | handover (PRAYOG-88) | Bug → PRAYOG-12 | `717f1fc` | `test_review_fixes` | pass |
| D3-10 | S1 approval (PRAYOG-98) | Bug → PRAYOG-14 | `717f1fc` | `test_review_fixes` | pass |
| D3-11 | FR-GPU-145 / active access | Bug → PRAYOG-6 | `717f1fc` | `test_review_fixes` (2) | pass |
| D3-12 | CI | Bug → PRAYOG-5 | `cc209d9` | CI run on PR #26 | green |
| D3-13 | NFR-GPU-030 | Bug → PRAYOG-9 | `d3-final` | `test_security`, probe A03b | pass |
| D3-14 | NFR-GPU-014 | Bug → PRAYOG-8 | `d3-final` | `test_adversarial` (3), `pass_benchmark` | pass |
| D3-15 | NFR-GPU-039 | Bug → PRAYOG-8 | `d3-final` | `test_adversarial` (2), `load_test` | pass |
| D3-16 | input validation | Bug → PRAYOG-15 | `d3-final` | `test_adversarial` (4) | pass |
| D3-17 | input validation | Bug → PRAYOG-9 | `d3-final` | `test_adversarial` (4) | pass |
| D3-18 | FR-ML-003 | Bug → PRAYOG-13 | `d3-final` | `test_adversarial` (1) | pass |
| D3-19 | FR-AUTH-28 | Bug → PRAYOG-6 | `d3-final` | `test_adversarial` (1) | pass |
| D3-20 | A-18 | Bug → PRAYOG-9 | `d3-final` | `test_security` (1) | pass |

# 5. Story-level matrix



In *Result*, a failure count is a Windows-only failure (see Coverage report section 4); `wr#` PRs are in `baymaxisme/Prayog`, `#` PRs in `Dishaswamy/Prayog`.

| Jira | Story / task | Requirement(s) | Status | Code (files citing the requirement) | GitHub | Automated tests | Result 6 Nov | Other evidence |
|---|---|---|---|---|---|---|---|---|
| PRAYOG-17 | Create GitHub repository, branch protection and PR template | — | Done | — | — | — | — | CI workflow `.github/workflows/mlflow-validation.yml`; branch protection on GitHub |
| PRAYOG-18 | Configure Jira–GitHub integration and smart commits | — | Done | — | — | — | — | smart-commit keys in branch names (PRAYOG-79…92) |
| PRAYOG-19 | Database schema and migrations for the core data model | — | Done | — | — | — | — | CI: `alembic upgrade head` on empty PostgreSQL + `alembic check`; repeated locally 6 Nov |
| PRAYOG-20 | Configuration module with startup invariant validation | NFR-GPU-053 | Done | `config.py` | 9fe8fa5 | `scheduler/test_config` | 15 pass | config validated at startup (`load_config`) |
| PRAYOG-21 | Structured logging and correlation IDs | NFR-GPU-049 | Done | — | — | — | — | no test found |
| PRAYOG-22 | Developer environment: Docker Compose with simulated node agents | NFR-GPU-033 | Done | `__init__.py`<br>`simulated.py` | — | — | — | docker-compose.yml (postgres, steward-runtime); simulated agents used in drills |
| PRAYOG-23 | Metrics endpoint | NFR-GPU-051 | Done | `metrics.py`<br>`passes.py`<br>`models.py` … | 9fe8fa5 | `scheduler/test_jobs_api` | 29 pass | `/metrics` exercised manually |
| PRAYOG-24 | Cloud deployment on the free tier, with a runbook | — | Done | — | — | — | — | not verified (Deployment section 9) |
| PRAYOG-25 | Register a Prayog account with institute email and password | FR-AUTH-01 | Done | — | — | `test_auth_routes`, `test_login` | 23 pass |  |
| PRAYOG-26 | Submit an enrolment request with purpose, Professor and period | FR-AUTH-05 | Done | — | #19 | `test_access_request_purpose` | 22 pass |  |
| PRAYOG-27 | See the status of my enrolment request | — | Done | — | — | `test_access_request_purpose` | 22 pass |  |
| PRAYOG-28 | Professor reviews and decides enrolment requests naming them | FR-AUTH-10 | Done | — | — | `test_access_request_purpose` | 22 pass |  |
| PRAYOG-29 | Platform Admin approves or rejects enrolment | FR-AUTH-14 | Done | — | — | `test_access_request_purpose` | 22 pass |  |
| PRAYOG-30 | Authenticate the CLI with prayog login | FR-AUTH-20 | Done | `cli.py` | #23, #24 | `test_cli_login`, `cli/test_login_logout` | 29 pass, 1 fail |  |
| PRAYOG-31 | Record each approved user's lab and account type | FR-GPU-139 | Done | — | #19 | `test_access_request_purpose`, `test_lab_scheduling` | 32 pass |  |
| PRAYOG-32 | Enrol a node agent with a one-time token and node-scoped credential | FR-GPU-110 | Done | `domain.py`<br>`nodes.py`<br>`models.py` | — | `test_gpu_agent_api` | 26 pass | drill F1 (re-start with stored credential) |
| PRAYOG-33 | Authorize every protected operation server-side | FR-AUTH-28, NFR-GPU-029 | Done | — | — | `test_review_fixes`, `test_auth_routes`, `cli/test_protected_commands` | 42 pass | probes A01–A06, A11 |
| PRAYOG-34 | Log out and have the server session end | FR-AUTH-47 | Done | — | — | `test_auth_routes`, `test_review_fixes` | 24 pass | probes A12–A13 |
| PRAYOG-35 | Time-bounded access that expires on its approved end date | FR-AUTH-36 | Done | — | — | `test_access_request_purpose`, `test_cli_login` | 31 pass |  |
| PRAYOG-36 | Distinguish expired authorization from wrong credentials | NFR-AUTH-09 | Done | — | — | `test_cli_login` | 9 pass |  |
| PRAYOG-37 | Retrieval survives expiry; commitment does not | FR-GPU-145 | Done | — | — | `test_review_fixes` | 14 pass |  |
| PRAYOG-38 | Request an access extension | FR-AUTH-42 | Done | — | — | — | — | no test found |
| PRAYOG-39 | Revoke a user's authorization | FR-AUTH-39 | Done | — | — | `test_access_request_purpose`, `test_auth_routes` | 32 pass |  |
| PRAYOG-40 | Prevent one person holding admin and submitting rights | FR-GPU-173 | Done | — | — | — | — | no test found |
| PRAYOG-41 | Deactivate a departed user's account | FR-AUTH-66 | Done | — | — | — | — | no test found |
| PRAYOG-42 | Review authentication and authorization audit records | FR-AUD-01 | Done | `auth.py` | — | `test_review_fixes`, `test_pool_picture`, `test_admin_cancel_job` | 24 pass |  |
| PRAYOG-43 | Professor keeps continuing institutional access | — | Done | — | — | — | — | no test found |
| PRAYOG-44 | Link a local project folder to Prayog | FR-GPU-001 | Done | — | #23, #24 | `cli/test_folder` | 19 pass, 1 fail |  |
| PRAYOG-45 | Capture an immutable, content-addressed project snapshot | FR-GPU-006 | Done | — | #23, #24 | `cli/test_folder`, `test_project_versions`, `test_staging` | 67 pass, 1 fail |  |
| PRAYOG-46 | Submit a GPU execution request for a linked project | FR-GPU-009 | Done | — | #6 | `scheduler/test_requests`, `scheduler/test_jobs_api`, `test_gpu_run_submission` | 113 pass |  |
| PRAYOG-47 | Declare GPU count and walltime, and validate them | FR-GPU-010 | Done | `requests.py` | b3d9c46 | `scheduler/test_requests`, `scheduler/test_jobs_api`, `test_gpu_run_submission` | 113 pass | probe A16 |
| PRAYOG-48 | Answer the submission on the spot | FR-GPU-017 | Done | `requests.py` | b3d9c46 | `scheduler/test_jobs_api`, `test_gpu_run_submission` | 53 pass | load test: POST /jobs p95 0.30 s |
| PRAYOG-49 | Eligibility evaluator | FR-GPU-030 | Done | — | #4 | `scheduler/test_eligibility`, `test_pass_equivalence`, `test_node_sharing` | 22 pass |  |
| PRAYOG-50 | Priority class resolver P0–P3 | FR-GPU-036 | Done | `classes.py` | af13dd1 | `scheduler/test_classes`, `test_lab_scheduling` | 19 pass |  |
| PRAYOG-51 | Fair-share scoring policy with deterministic ordering | FR-GPU-043 | Done | `passes.py` | 9739a3a | `scheduler/test_scoring`, `test_pass_equivalence`, `test_lab_scheduling` | 40 pass |  |
| PRAYOG-52 | Scheduling pass orchestrator and its triggers | FR-GPU-121 | Done | `steward_bridge.py`<br>`passes.py`<br>`run.py` … | #22 | `scheduler/test_passes`, `test_pass_equivalence`, `test_rebuild_scheduling_path` | 34 pass | drill F4 (event-driven pass without the tick) |
| PRAYOG-53 | Atomic claim transaction with the owner-claim guard | NFR-GPU-001, FR-GPU-051 | Done | `claim.py` | #5, #21 | `scheduler/test_claim`, `scheduler/test_allocation_properties`, `test_lab_scheduling`, `test_concurrency` | 44 pass | concurrency C3, C5, C6 |
| PRAYOG-54 | Show why a queued job is waiting, and until when | FR-GPU-021 | Done | `eligibility.py`<br>`requests.py`<br>`jobs.py` | #26 | `scheduler/test_waiting`, `scheduler/test_jobs_api`, `test_job_list_batched`, `test_web_app` | 49 pass |  |
| PRAYOG-55 | Refuse a submission that would exceed the group's GPU-hour cap | FR-GPU-104 | Done | `requests.py` | 9fe8fa5 | `scheduler/test_requests`, `scheduler/test_adversarial`, `test_admin_cancel_job` | 87 pass | quota path only indirectly |
| PRAYOG-56 | Show the group's remaining cap and per-member consumption | FR-GPU-178 | Done | `requests.py`<br>`jobs.py`<br>`research.js` | 9fe8fa5 | `scheduler/test_requests`, `scheduler/test_jobs_api` | 89 pass | |
| PRAYOG-57 | Edit a queued job without farming queue age | FR-GPU-070 | Done | `requests.py` | 9fe8fa5 | `scheduler/test_requests` | 60 pass | |
| PRAYOG-58 | Resubmit a past run with changed resources | FR-GPU-133 | Done | — | #25 | `test_gpu_rebuild` | 22 pass |  |
| PRAYOG-59 | Dispatch an allocation idempotently to the node agent | NFR-GPU-019, FR-GPU-058 | Done | `dispatch.py` | #9 | `scheduler/test_dispatch`, `scheduler/test_agent_api`, `test_gpu_agent_api` | 64 pass |  |
| PRAYOG-60 | Node agent materialises the workspace and executes the run | FR-GPU-062 | Done | — | fe073a0 | `scheduler/test_agent_api`, `test_node_agent_units`, `test_node_agent_docker` | 67 pass | CI hardened-container step |
| PRAYOG-61 | Enforce walltime and stop safely | FR-GPU-073 | Done | `reaper.py` | fe073a0 | `scheduler/test_reaper`, `scheduler/test_agent_api`, `test_node_agent_runs` | 49 pass |  |
| PRAYOG-62 | Check what my job is doing right now | FR-GPU-018 | Done | — | #26 | `scheduler/test_jobs_api`, `scheduler/test_agent_api`, `test_web_app`, `test_job_list_batched` | 60 pass | load test GET /jobs/{id} |
| PRAYOG-63 | Cancel a job in any state | FR-GPU-065 | Done | `cancel.py`<br>`models.py` | fe073a0 | `scheduler/test_dispatch`, `scheduler/test_adversarial`, `scheduler/test_security`, `test_concurrency`, `test_review_fixes` | 86 pass | concurrency C5 |
| PRAYOG-64 | Requeue a job interrupted by hardware without losing its place | FR-GPU-077 | Done | `domain.py`<br>`nodes.py`<br>`reaper.py` | — | `scheduler/test_reaper`, `scheduler/test_liveness`, `test_gpu_run_finalize`, `test_review_fixes` | 57 pass | drill F1 |
| PRAYOG-65 | Account GPU-hours actually consumed | FR-GPU-097 | Done | — | #7 | `scheduler/test_lifecycle`, `scheduler/test_lifecycle_edges`, `test_lab_scheduling`, `test_pool_picture` | 45 pass |  |
| PRAYOG-66 | Return the outcome and logs of every terminal run | FR-GPU-091 | Done | — | — | `test_gpu_run_finalize` | 17 pass |  |
| PRAYOG-67 | Notify the owner of maintenance that will stop their job | FR-GPU-109 | Done | `requests.py`<br>`jobs.py` | — | `test_job_list_batched` | 3 pass | planned maintenance stop in the job view |
| PRAYOG-68 | Tell the owner when no machine can ever run their queued job | FR-GPU-117 | Done | `nodes.py`<br>`reaper.py` | — | `scheduler/test_nodes` | 31 pass | |
| PRAYOG-69 | Notification records surfaced by the CLI | FR-GPU-176 | Done | `maintenance.py`<br>`nodes.py` | — | — | — | no test found |
| PRAYOG-70 | Capture run outputs and store them server-side | NFR-GPU-022, FR-GPU-086 | Done | — | — | `test_gpu_run_finalize`, `test_mlflow_artifacts` | 19 pass |  |
| PRAYOG-71 | Synchronize results into the project folder automatically | FR-GPU-091 | Done | — | #25 | `cli/test_runs` | 66 pass, 1 fail |  |
| PRAYOG-72 | Report exactly which files block a synchronization | FR-GPU-177 | Done | — | #25 | `cli/test_runs` | 66 pass, 1 fail |  |
| PRAYOG-73 | Register GPU nodes and their units | FR-GPU-025 | Done | `nodes.py` | fe073a0 | `scheduler/test_nodes`, `scheduler/test_agent_api`, `test_gpu_agent_api`, `test_pool_picture` | 79 pass |  |
| PRAYOG-74 | Alert when a node fails to come online or stops reporting | FR-GPU-174 | Done | `domain.py`<br>`nodes.py`<br>`models.py` | fe073a0 | `scheduler/test_nodes`, `scheduler/test_reaper`, `scheduler/test_liveness` | 57 pass | drill F1/F4: NODE_DOWN alerts raised |
| PRAYOG-75 | Schedule, narrow or cancel maintenance windows | FR-GPU-081 | Done | `maintenance.py` | fe073a0 | `scheduler/test_agent_api`, `scheduler/test_reaper`, `test_pool_picture`, `test_hardware_investigation` | 48 pass |  |
| PRAYOG-76 | Take a node out of service for repair | FR-GPU-080 | Done | `config.py`<br>`domain.py`<br>`nodes.py` … | fe073a0 | `scheduler/test_nodes`, `scheduler/test_reaper`, `test_hardware_investigation` | 57 pass |  |
| PRAYOG-77 | Return a repaired node to service | FR-GPU-169 | Done | `nodes.py`<br>`models.py` | fe073a0 | `scheduler/test_nodes`, `test_hardware_investigation` | 40 pass |  |
| PRAYOG-78 | Decommission a node permanently | FR-GPU-112 | Done | `nodes.py` | fe073a0 | `scheduler/test_nodes`, `scheduler/test_reaper`, `test_node_sharing` | 53 pass |  |
| PRAYOG-79 | Change a node's donation mode and teaching-pool flag | FR-GPU-111 | Done | `nodes.py` | #14 | `test_node_sharing` | 5 pass |  |
| PRAYOG-80 | Read-only operational picture of the pool | FR-GPU-156 | Done | `overview.py`<br>`pool.js` | #15, #26 | `test_pool_picture` | 4 pass |  |
| PRAYOG-81 | Cancel any user's job with a recorded reason | FR-GPU-065 | Done | `cancel.py`<br>`models.py` | #13 | `test_admin_cancel_job` | 6 pass |  |
| PRAYOG-82 | Investigate a suspected hardware fault | FR-GPU-158 | Done | `hardware_investigation.py`<br>`config.py`<br>`app.py` … | #16 | `test_hardware_investigation` | 9 pass |  |
| PRAYOG-83 | Capture metrics continuously and seal snapshots automatically | FR-ML-001 | Done | — | #12, wr#32 | `test_mlflow_lifecycle`, `test_mlflow_metric_batch` | 30 pass |  |
| PRAYOG-84 | Outbox relay and idempotent ledger consumer | FR-ML-001 | Done | — | #12, wr#33 | `test_mlflow_outbox`, `test_mlflow_ledger_consumer`, `test_mlflow_outbox_worker` | 18 pass |  |
| PRAYOG-85 | Snapshot state machine with INCOMPLETE | FR-ML-007 | Done | — | #12, wr#41 | `test_mlflow_lifecycle`, `test_mlflow_snapshot_storage_failures` | 29 pass |  |
| PRAYOG-86 | Professor reads the ledger of students naming them | FR-ML-004 | Done | `mlflow_professor_ledger.py`<br>`professor.js` | #12, #26, wr#37 | `test_mlflow_professor`, `test_mlflow_professor_api`, `test_mlflow_professor_ledger`, `test_mlflow_professor_postgres`, `test_mlflow_professor_redaction` | 26 pass |  |
| PRAYOG-87 | Admin sees volume and availability, never content | FR-ML-005 | Done | `app.py`<br>`config.py` | #12, wr#29 | `test_admin_mlflow` | 2 pass |  |
| PRAYOG-88 | Hand over project IP to the supervising Professor | FR-ML-008 | Done | `research.js` | #12, #26, wr#34 | `test_mlflow_handover`, `test_review_fixes` | 19 pass |  |
| PRAYOG-89 | Trigger an environment rebuild from a Snapshot ID | FR-ML-003 | Done | `c3a8d5e1f7b2_steward_integration.py`<br>`rebuild.py`<br>`models.py` | #12, wr#30 | `test_gpu_rebuild`, `test_mlflow_rebuild_preinit` | 25 pass |  |
| PRAYOG-90 | Rebuild attempt state machine and the pre-init boundary | FR-ML-003, FR-STW-001 | Done | `c3a8d5e1f7b2_steward_integration.py`<br>`gpu_runs.py`<br>`rebuild.py` … | #12, wr#31 | `test_mlflow_rebuild_preinit`, `test_mlflow_lifecycle` | 28 pass |  |
| PRAYOG-91 | Submit-by-snapshot through the ordinary scheduling path | — | Done | — | #12, #17 | `test_rebuild_scheduling_path`, `test_mlflow_queue_order` | 10 pass |  |
| PRAYOG-92 | Metric comparator with per-metric tolerances | — | Done | — | #12, #18 | `test_mlflow_compare` | 8 pass |  |
| PRAYOG-93 | RAG pipeline with an authorization-bound context window | NFR-SEC-002, FR-STW-015 | Done | `setup_steward.py`<br>`engine.py`<br>`0001_initial_schema.py` … | #11, wr#36 | `test_steward_context` | 5 pass | Steward suite (outside `main`) |
| PRAYOG-94 | Diagnostic link when a rebuild fails | NFR-SEC-003, FR-STW-003 | Done | `cli.py`<br>`links.py`<br>`s3.py` | #11, #26, wr#39 | `test_diagnostic_session_audit`, `test_cli_rebuild_reaches_steward` | 7 pass, 1 fail | Steward suite |
| PRAYOG-95 | Run-failure diagnosis with cited evidence | FR-GPU-146 | Done | `c3a8d5e1f7b2_steward_integration.py`<br>`lifecycle.py`<br>`models.py` … | #11, #26 | `tests_steward_check` | 17 pass | Steward suite (`steward/tests`, kept outside `main` at the team lead's request) |
| PRAYOG-96 | Two-stage sanitiser for ingestion and outbound search | NFR-SEC-006, FR-STW-011 | Done | `ports.py`<br>`sanitiser.py`<br>`search.py` … | #11 | `tests_steward_check` | 17 pass | Steward suite (`steward/tests`, kept outside `main` at the team lead's request) |
| PRAYOG-97 | Output guard: no fabrication, no command execution | FR-GPU-151, FR-STW-013 | Done | `__init__.py`<br>`app.py`<br>`service.py` … | #11 | `tests_steward_check` | 17 pass | Steward suite (`steward/tests`, kept outside `main` at the team lead's request) |
| PRAYOG-98 | Approve or reject a Steward-proposed corrected job | FR-GPU-149 | Done | `proposals.py`<br>`owner.py`<br>`s1.py` | #11, #26 | `tests_steward_check` | 17 pass | Steward suite (`steward/tests`, kept outside `main` at the team lead's request) |
| PRAYOG-99 | Cross-user fault correlation and hardware warnings | FR-GPU-159 | Done | `s2.py` | #11 | `tests_steward_check` | 17 pass | Steward suite (`steward/tests`, kept outside `main` at the team lead's request) |
| PRAYOG-100 | Optimisation advisory after a successful rebuild | FR-STW-006 | Done | `gpu_runs.py`<br>`cli.py`<br>`s4.py` … | #11 | `tests_steward_check` | 17 pass | Steward suite (`steward/tests`, kept outside `main` at the team lead's request) |
| PRAYOG-101 | Prove per-user isolation of ingestion, retrieval and caching | NFR-SEC-001 | Done | `0001_initial_schema.py` | #11 | `tests_steward_check` | 17 pass | Steward suite (`steward/tests`, kept outside `main` at the team lead's request) |
| PRAYOG-102 | Degrade safely when the agent is unavailable | NFR-AVAIL-001, NFR-GPU-055, FR-STW-010 | Done | `steward_bridge.py`<br>`steward_worker.py`<br>`lifecycle.py` … | #11 | `tests_steward_check` | 17 pass | Steward suite (`steward/tests`, kept outside `main` at the team lead's request) |
| PRAYOG-103 | Unit test suite and coverage gate | NFR-GPU-054 | In Progress | `__init__.py`<br>`run.py` | — | `test_concurrency`, `test_job_list_batched`, `test_database_unavailable` | 11 pass | Test and Coverage reports |
| PRAYOG-104 | Concurrency harness for the claim | NFR-GPU-001 | In Progress | — | — | `scheduler/test_claim`, `scheduler/test_adversarial`, `scheduler/test_passes`, `test_concurrency` | 79 pass | Concurrency report; `backend/experiments/` |
| PRAYOG-105 | Property tests for the fairness and allocation invariants | NFR-GPU-009 | To Do | `config.py` | — | `scheduler/test_scoring`, `scheduler/test_allocation_properties`, `test_pass_equivalence` | 31 pass | property-style equivalence of passes; no Hypothesis suite |
| PRAYOG-106 | Integration and end-to-end suite | — | To Do | — | 59db073, 9838d1a, 33d2884 | `test_mlflow_e2e`, `test_cli_end_to_end` | 3 pass | CI e2e step |
| PRAYOG-107 | Adversarial testing | — | To Do | — | — | `scheduler/test_adversarial` | 21 pass | Security report section 7 (37 adversarial cases) |
| PRAYOG-108 | Security testing and threat model | — | To Do | — | — | `scheduler/test_security` | 25 pass | Security report |
| PRAYOG-109 | Performance and load experiment with one optimization | NFR-GPU-014 | To Do | — | — | `scheduler/test_adversarial`, `test_job_list_batched` | 24 pass | Performance report; `backend/experiments/` |
| PRAYOG-110 | Failure and recovery testing | NFR-GPU-042 | To Do | — | — | `scheduler/test_liveness`, `test_database_unavailable`, `test_concurrency` | 17 pass | Failure and Recovery report; `backend/experiments/` |
| PRAYOG-111 | Five meaningful defects found, fixed and regression-tested | — | To Do | — | — | `scheduler/test_adversarial`, `scheduler/test_security`, `scheduler/test_liveness` | 55 pass | Defect history (13 defects) |
| PRAYOG-112 | AI Engineering Log — architecture activities | — | Done | — | 9efaf63 | — | — | AI Engineering Log (13) |
| PRAYOG-113 | Deliverable 2 package | — | In Progress | — | 04b6cee | — | — | DELIVERABLE_2_PACKAGE.md in the repository |
| PRAYOG-114 | Update the traceability matrix to implementation and tests | — | To Do | — | — | — | — | this matrix (11) |
| PRAYOG-115 | Cost estimation, effort model and Gantt chart | — | To Do | — | — | — | — | Costing (08), Gantt (09) |
| PRAYOG-116 | Deliverable 3 package | — | To Do | — | — | — | — | this D3 package (00–13) |
| PRAYOG-117 | Scenario-driven demonstration rehearsal | — | To Do | — | — | — | — | Sprint 3 |
| PRAYOG-118 | Engineering retrospective and final report | — | To Do | — | — | — | — | Project report (12) |
| PRAYOG-119 | Final traceability and Jira dashboard | — | To Do | — | — | — | — | this matrix (11), dashboard (12) |