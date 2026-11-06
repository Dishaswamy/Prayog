# Test Cases

This directory contains the automated test suite for the Prayog backend and its CLI-to-server flows. Tests are collected and run with `pytest`.

## Layout

```
test_cases/
  conftest.py     puts backend/ and the repo root on sys.path
  helpers/        shared fixtures (scheduler_pg, gpu_case, agent_case, access_api_case, reference_pass)
  auth/           login, registration, access requests, password reset, JWT dependencies
  mlflow/         experiments, artifacts, handover, outbox/ledger, professor views, rebuild, worker
  gpu/            scheduler, node agent, GPU runs, project versions, staging, node sharing
  e2e/            cross-cutting flows: CLI-to-server, API-to-worker, Steward, web app
```

## Run the tests

From the repository root:

```bash
python -m pip install -r backend/requirements.txt
python -m pytest test_cases/
```

One domain, one module, or tests by name:

```bash
python -m pytest test_cases/auth/
python -m pytest test_cases/gpu/test_staging.py
python -m pytest test_cases/ -k role_guard
```

The suite contains unit tests, mocked route/logic tests, and selected integration-oriented tests. Some integration tests require PostgreSQL or Docker and may be skipped or fail when those services are unavailable. The suite is not a substitute for deployment validation against the production database, object store, Docker runtime, or GPU infrastructure.

## Coverage areas

The current test modules cover:

- Authentication and authorization: email normalization, registration restrictions, password verification, JWT validation, database-backed roles, session identifiers, role guards, and admin-only node-agent management routes.
- Administrative and professor workflows: admin ML Flow views, professor API permissions, professor ledger filtering, and PostgreSQL-backed professor behavior.
- ML Flow lifecycle: run transitions, resubmission, project snapshots, rebuild initialization, snapshot completeness, artifact association, and route registration.
- Worker and scheduling behaviour: queue ordering, worker claims, stale-claim recovery, node capability matching, cancellation, Docker command isolation, worker-managed execution, node-agent enrollment, heartbeat authentication, credential revocation, and security-event auditing.
- Transactional outbox and ledger processing: event validation, lease handling, retry behaviour, worker polling, consumer idempotency, and ledger persistence.
- Artifact handling: content-addressed integrity checks, owner-scoped upload, inventory, and download authorization, professor metadata-only access, private response headers, archive extraction safety, path traversal, symlinks, cache validation, and storage failure handling.
- Handover and access control: authorization, visibility, approval/rejection/cancellation behaviour, and artifact inventory verification.
- Steward and diagnostic safeguards: context retrieval constraints, redaction, diagnostic-link ownership/expiry/session audit, and artifact integrity.
- Metric processing and comparison: batch limits, duplicate metric names, tolerance boundaries, and invalid values.
- CLI behavior and end-to-end workflows: command handling, API-to-worker execution, rebuild outcomes, and cleanup where the required services are available.

## Test modules

See the subfolders above; each file is named `test_<area>.py`.

## Important limitations

A passing test run establishes only that the collected tests passed in that environment. It does not establish production readiness or validate all concurrency and failure behaviour against real PostgreSQL transactions, S3-compatible storage, Docker, or GPU hardware. Tests that need external services must be run with those services configured; skipped tests should be reported as skipped, not as passed.

No test result is implied by this README. Record the actual command, environment, and result when the suite is run.
