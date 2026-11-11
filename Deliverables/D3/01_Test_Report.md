---
title: "Prayog — Unit, Integration, System and Adversarial Test Report"
subtitle: "CS455 Deliverable 3 · Group 3 · 6 November 2026"
---

# 1. Summary

**Final run, on Linux, of the code in this package.** Backend and CLI were run in a `python:3.12` container, the same OS and Python as CI, on PostgreSQL 16. The Steward and harness figures are from Areen's run the same day; that code did not change, and its tests are not in this repository.

| Suite | Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| Backend, including the scheduler suite below | 878 | 867 | 0 | 11 (Docker; these run in CI) |
| CLI | 165 | 165 | 0 | 0 |
| Steward | 222 | 222 | 0 | 0 |
| Backend–Steward harness | 17 | 17 | 0 | 0 |
| **Total automated** | **1282** | **1271** | **0** | **11** |

Beyond these: 37 of 37 adversarial probes pass on the final build; six live failure scenarios at production timeouts plus Areen's four drills; two load experiments on three builds; and the concurrency experiment.

**By level, from Areen's run earlier on 6 November (Windows):** the table in section 1.1 breaks that run down by level. Sixteen tests failed there for platform reasons; the Linux run above confirms that none of them fail on Linux.

## 1.1 Areen's run by level

| Level | Suites | Tests | Passed | Failed | Skipped | Where |
|---|---|---|---|---|---|---|
| Unit | Steward unit; CLI (fake HTTP server); backend component tests (archive handling, scheduler policy, ledger state machines) | 136 + 165 + ≈ 190 | all but 9 (6 backend, 3 CLI; Windows-only) | 9 | — | local |
| Integration (API + PostgreSQL) | backend API/route tests; Steward on PostgreSQL with row-level security; backend ↔ Steward harness | ≈ 290 + 86 + 17 | all but 6 (Windows-only) | 6 | — | local |
| Concurrency | `test_concurrency.py` | 6 | 6 | 1 → 0 | — | local, 5 runs |
| System / end-to-end | CLI → real API; API → Docker worker e2e; hardened node-agent container; worker smoke | 3 + 4 + 4 + 3 | 3 locally; 11 in CI | 0 | 11 locally (no Docker) | local + CI |
| Adversarial | black-box probes against the live API | 37 cases | 36 | 1 (D3-13, since fixed) | — | local |

Every failure was investigated: the backend's 13 and the CLI's 3 fail only on Windows (symlink privilege, `os.killpg`, POSIX permission bits, path separators) and pass in Linux CI, except one assertion in a new D3 test that was corrected (section 4). **No product test fails for a product reason.** The non-functional techniques found eight product defects that the functional suites did not (Defect history).

Environment: Windows 11, Python 3.12.8, PostgreSQL 16 on a local cluster (port 55432), fresh migrated databases per suite (`prayog_d3_test`, `prayog_d3_final`, `prayog_d3_conc`; the Steward and harness create their own). Code: `main` `18767aa` + branch `d3-testing`. CI (GitHub Actions, Ubuntu): green on PR #26 head `cc209d9` — CLI on Python 3.9 and 3.12, backend unit tests, migrations, `alembic check`, Docker worker image, Docker smoke, hardened node-agent container, API-to-worker e2e.

# 1.2 The GPU scheduler suite

These tests were written with the scheduler and held back for this deliverable, then updated for the code as it now stands. They are in `test_cases/gpu/scheduler/` and run in CI.

| Module | Tests passing |
|---|---|
| `test_adversarial` | 21 |
| `test_agent_api` | 18 |
| `test_allocation_properties` | 1 |
| `test_claim` | 27 |
| `test_classes` | 9 |
| `test_config` | 15 |
| `test_dispatch` | 20 |
| `test_eligibility` | 15 |
| `test_jobs_api` | 29 |
| `test_lifecycle` | 22 |
| `test_lifecycle_edges` | 9 |
| `test_liveness` | 9 |
| `test_nodes` | 31 |
| `test_passes` | 25 |
| `test_reaper` | 17 |
| `test_requests` | 60 |
| `test_scoring` | 28 |
| `test_security` | 25 |
| `test_seed_demo` | 1 |
| `test_waiting` | 7 |
| **Total** | **389** |

Their quality was checked by mutation testing while the scheduler was being built: 51 bugs were deliberately introduced, 49 were caught, and the other 2 changed no behaviour. Four tests were added because a bug survived at first.

Beyond the functional tests, `test_adversarial`, `test_security`, `test_liveness` and `test_allocation_properties` are new in this deliverable. Their results before the fixes are in `evidence/adversarial_before_fix.txt`.

# 2. Unit tests

| Area | Module(s) | Tests | What they pin down |
|---|---|---|---|
| Steward logic | `steward/tests/unit` | 136 | sanitiser (secrets, PII, code removed before egress), citation validator, output guard (no commands, no fabrication), proposal limits (GPU count and walltime only), heuristic diagnosis, knowledge-base chunking |
| CLI | `cli/tests` (6 modules) | 165 | login/logout and credential file, access gating, project linking and snapshots (manifest, fingerprints, refusal of symlinks), `prayog run` / `--rebuild-of` / result sync |
| Untrusted archives and run outputs | `test_node_agent_units` (49), `test_staging` (27), `test_mlflow_worker` (7) | 83 | chunk and file checksums, manifest digests, path traversal, symlinks, entrypoint outside the code, read-only extraction, output shipping rules |
| Scheduler policy | `test_pass_equivalence` (2), `test_lab_scheduling` (10) | 12 | eligibility, priority classes, fair-share score, deterministic ties, bucketed pass ≡ reference pass |
| Ledger state machines | `test_mlflow_lifecycle` (25), `test_mlflow_outbox` (10), `test_mlflow_compare` (8), `test_mlflow_metric_batch` (5), `test_mlflow_ledger_consumer` (5), `test_project_versions` (21) | 74 | run/snapshot/rebuild transitions, INCOMPLETE reasons, idempotent outbox consumption, metric tolerances |

# 3. Integration tests (API + PostgreSQL)

| Area | Modules (tests) | Result |
|---|---|---|
| Authentication and sessions | `test_auth_routes` (10), `test_login` (13), `test_password_reset` (15), `test_cli_login` (9), `test_review_fixes` (14) | all pass |
| Access requests and authorisation | `test_access_request_purpose` (22), `test_admin_cancel_job` (6), `test_admin_mlflow` (2), `test_node_sharing` (5), `test_hardware_investigation` (9), `test_pool_picture` (4) | all pass |
| GPU jobs and agents | `test_gpu_run_submission` (24), `test_gpu_agent_api` (26), `test_gpu_run_finalize` (17), `test_gpu_rebuild` (22), `test_rebuild_scheduling_path` (7), `test_node_agent_runs` (14), `test_web_app` (10), `test_job_list_batched` (3, new) | pass except 3 Windows-only (`test_node_agent_runs`) |
| Ledger, professor and handover | `test_mlflow_professor*` (26), `test_mlflow_handover` (5), `test_mlflow_routes` (1), `test_mlflow_artifacts` (2), `test_mlflow_snapshot_storage_failures` (4), `test_mlflow_queue_order` (3), `test_mlflow_rebuild_preinit` (3), `test_mlflow_outbox_worker` (3), `test_project_names` (3), `test_artifact_storage` (5) | pass except 2 Windows-only (`test_artifact_storage`) |
| Steward hand-offs | `test_steward_context` (5), `test_diagnostic_session_audit` (6), `test_cli_rebuild_reaches_steward` (2) | pass except 1 Windows-only path separator |
| Failure handling | `test_database_unavailable` (2, new) | pass |
| Steward on PostgreSQL (RLS role that cannot bypass policies) | `steward/tests/integration` (86) | all pass |
| Backend ↔ Steward (seal hook, S1 diagnosis, S3 links, S4 advice through the real API) | `backend/tests_steward_check` (17) | 17 pass |

# 4. System tests

**End-to-end (automated).** `test_cli_end_to_end` drives the real `prayog` CLI against a running API (login, link, snapshot, submit, status, results): 3 pass locally. The API-to-worker e2e (`test_mlflow_e2e`, 4), real Docker worker smoke (`test_mlflow_worker_docker`, 3) and hardened node-agent container (`test_node_agent_docker`, 4) need Docker; they are skipped on the test laptop and **pass in CI**.

**Live system tests (this deliverable).**

| Test | What ran | Result | Report |
|---|---|---|---|
| Failure drills F1–F4 | API + tick + simulated agent + PostgreSQL; hard kills of each | every component recovered with no data loss; found D3-06 and D3-08 | 06 |
| Load test | 5 scenarios, 1–64 virtual users, baseline vs optimised | found D3-04 and D3-07; 18× faster job list, collapse at 64 users removed | 05 |
| Concurrency | 6 barrier-synchronised races | found D3-03 | 03 |

**The one failure in the final run that was not Windows-specific** was in new test C3: on the shared test database the scheduling passes also placed its jobs on other tests' shared nodes, which its last assertion did not expect. The assertion now counts live allocations on any node; C3 passes on the shared and the dedicated database. No product code changed.

# 5. Adversarial tests

Adversarial cases exist at three levels:

1. **In the suites** — malicious archives (zip slip, symlinks, manifest/inventory mismatch, wrong chunk length), foreign-node agents, replayed or session-mismatched diagnostic links, rebuild requests for foreign or incomplete snapshots (answered with the same generic rejection), professor reads outside supervision, bearer tokens redacted from logs: about 60 tests across `test_node_agent_units`, `test_staging`, `test_gpu_agent_api`, `test_diagnostic_session_audit`, `test_mlflow_rebuild_preinit`, `test_mlflow_professor_redaction`.
2. **Concurrency as an attack** — 25 parallel password guesses cannot beat the lockout (C4); 16 duplicate and 40 parallel submissions cannot beat idempotency or the queue limit (C1, C2).
3. **Black-box probes** — 37 cases against the live API: IDOR and existence oracles, privilege escalation (student → admin, user token → agent API, forged role claim), token forgery (`alg=none`, guessed key, tampered payload, expired, reused after logout), SQL injection in login, oversized and malformed input, mass assignment, path traversal into the static web app, brute force, CORS, security headers, error leakage. **37 of 37 pass on the final build.** A03b failed on the earlier build and passes now that D3-13 is fixed. Full table: Security report section 7.

# 6. How to reproduce

```bash
cd backend
export POSTGRES_USER=postgres POSTGRES_PASSWORD=… POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 POSTGRES_DB=prayog_test
export JWT_SECRET_KEY=test-only STEWARD_ENABLED=false PRAYOG_RUN_PROFESSOR_POSTGRES_TESTS=1 PRAYOG_RUN_SCHEDULER_POSTGRES_TESTS=1
alembic upgrade head && alembic check
pytest -q --cov=. --cov-report=xml tests                      # backend (478)
cd ../cli && pytest -q --cov=prayog                           # CLI (165)
cd ../steward && STEWARD_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/prayog pytest -q   # Steward (222)
```

Result files: `evidence/junit_*.xml` (one per suite), `evidence/cov_*.xml`.
