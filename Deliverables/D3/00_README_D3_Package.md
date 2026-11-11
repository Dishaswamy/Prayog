---
title: "Prayog — Deliverable 3 Package"
subtitle: "CS455 · Group 3 · Testing, Security, Performance, Cost, Deployment and Reliability · 6 November 2026"
---

# 1. Contents — mapped to the deliverable's list

| Required item | Document | Key result |
|---|---|---|
| Unit, integration, system and adversarial test reports | `01_Test_Report.pdf` | 1282 automated tests: 1271 pass, 0 fail, 11 skipped (Docker, run in CI); 37 of 37 adversarial probes pass on the final build |
| Code coverage report | `02_Code_Coverage_Report.pdf` | production code 85.4% overall: backend 84.2%, Steward 86.6%, CLI 91.0% |
| Concurrency test report | `03_Concurrency_Test_Report.pdf` | 6 barrier-synchronised races; live experiment through four API workers with 0 double allocations; without protection, 99 of 100 rounds double-book |
| Security / threat-model report | `04_Security_Threat_Model.pdf` | STRIDE over 6 trust boundaries (27 threats); bandit triage; pip-audit clean; one authorization hole found and fixed (D3-18) |
| Performance / load test report and optimisation results | `05_Performance_Load_Report.pdf` | `GET /jobs` 18× faster; four overlapping passes at 400 jobs 52 s → 1.9 s; 100 users 6.8 → 97.1 req/s, 136 → 0 errors |
| Failure / recovery test report | `06_Failure_Recovery_Report.pdf` | Areen's 4 drills plus 6 scenarios at production timeouts; a 45 s API outage no longer evicts running jobs (D3-08 fixed) |
| Bug / defect history in Jira | `07_Defect_History.pdf` | `evidence/jira_bugs_import.csv`: 20 defects, ready to import as Jira Bugs (**the import is still to be done**) |
| Evidence of ≥ 5 meaningful defects found and fixed | `07_Defect_History.pdf` section 2–3 | 19 fixed with regression tests, 0 open; failing runs saved in `evidence/adversarial_before_fix.txt` |
| Costing document | `08_Costing.pdf` | ≈ ₹8.2 lakh effort to date; ≈ ₹0/month on IITK infrastructure; ≈ $40/month on one cloud VM; ≈ $100/month Steward on Claude Opus 5.5 |
| Timeline / Gantt chart | `09_Timeline_Gantt.pdf` | planned vs actual for 13 workstreams |
| Deployment documentation | `10_Deployment_Guide.pdf` | Docker image and compose stack verified locally; cloud runbook; systemd and nginx route; smoke checks, upgrades, backups. **Not yet deployed to a cloud service** |
| Final requirements → Jira → GitHub → testing traceability matrix | `11_Traceability_Matrix.pdf` | 10 critical requirements end to end; 103 stories/tasks, 82% mapped to tests |
| Final Jira dashboard and project report | `12_Jira_Dashboard_Project_Report.pdf` | dashboard, status, sprint history, retrospective |
| AI Engineering Log | `13_AI_Engineering_Log.pdf` | whole-project log; D3 session with 8 AI errors caught |

# 2. Code that goes with this package

- Repository: `https://github.com/Dishaswamy/Prayog`, `main` at `18767aa` (PR #26 merged 5 Nov).
- D3 fixes and tests: commit `f358e01` on branch `d3-testing` (`claim.py`, `requests.py`, `jobs.py`, `main.py`, `database.py`, `workspace.py` and three new test modules), then branch `d3-final` on top of it: the GPU scheduler test suite, the fixes for D3-08 and D3-13 to D3-20, `backend/experiments/`, `backend/Dockerfile` and `docker-compose.deploy.yml`.

Paths in these documents follow the repository layout after PR #28 (6 Nov), which moved the scheduler to `backend/logic/gpu/scheduler/` and the tests to `test_cases/`. The D3 work was ported onto that layout in branch `d3-final`.

# 3. Evidence folder

| File | What |
|---|---|
| `junit_*.xml`, `cov_*.xml`, `cov_backend_html/` | raw test results and coverage |
| `load_baseline.json`, `load_optimized.json`, `load_table.md`, `loadgen.py`, `seed_load.py` | load test inputs and outputs |
| `failure_drills.py`, `failure_drills.json`, `failure_drills_after.json` | Areen's fault-injection script and timelines |
| `failure_before_outages.json`, `failure_after.json` | failure scenarios at production timeouts (script: `backend/experiments/failure_experiment.py`) |
| `pass_main.json`, `pass_d3testing.json`, `pass_final.json`, `load_main.json`, `load_d3testing.json`, `load_final.json` | the same benchmarks on three builds (`backend/experiments/`) |
| `concurrency_results.json` | the live concurrency experiment |
| `junit_backend_linux.xml`, `coverage_backend_linux.xml`, `coverage_cli_linux.xml` | the final Linux run |
| `adversarial_before_fix.txt`, `adversarial_after_fix.txt` | regression tests failing before the fixes, and passing after |
| `adversarial.py`, `adversarial.json` | probes and results |
| `bandit.json`, `pip_audit_backend.json` | static analysis and dependency audit |
| `traceability.json`, `traceability_matrix.md` | generated matrix data |
| `jira_bugs_import.csv` | the defect register as a Jira CSV import |

The scripts under `src/` regenerate the figures, the matrix and the PDFs from these files.

# 4. Honest limits

- The final backend and CLI runs were on Linux, in a `python:3.12` container. On Windows, 16 tests fail for platform reasons. The 11 Docker-based tests are skipped locally and run in CI. The Steward's 222 tests are not in this repository, so that figure comes from Areen's run.
- Load numbers come from one laptop running generator, API and database together, one run per configuration.
- **The platform has not been deployed to a cloud service** (course section 16). The image and compose stack run locally, and the runbook is in the Deployment guide, but this needs a team member's cloud account.
- **Jira:** the 20 defects are not imported yet, and the dashboard screenshot is not taken. Both need someone signed in.
- The Jira figures are from the 16 October export unless a newer export is noted in the Project report; the live Jira dashboard screenshot has to be taken by a team member signed in to Jira.
- Not verified: a live Claude call, a real GPU node, a cloud deployment, the systemd/nginx/TLS route, a backup restore.

# 5. Re-verification after the restructure (11 November)

The package above was submitted on 6 November. On 11 November the D3 work was ported onto `main` after PR #28 restructured the repository (branch `d3-final`), and the full suite was run again on Linux:

| Suite | Result | Coverage |
|---|---|---|
| Backend (`test_cases/`, including the 389 scheduler tests) | 967 passed, 0 failed, 11 skipped (Docker; run in CI) | 86% of backend production code |
| CLI | 291 passed, 0 failed (more tests were added to `main` after 6 November) | 99% |

Results: `evidence/junit_backend_ported_11nov.xml`, `evidence/coverage_backend_ported_11nov.xml`, `evidence/junit_cli_ported_11nov.xml`. The measured figures in the reports are from 6 November and were not re-run.
