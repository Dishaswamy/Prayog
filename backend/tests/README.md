# Backend Test Suite

This directory contains the automated test suite for the Prayog backend. Tests are collected and run with `pytest`.

## Run the tests

From the repository root:

```bash
cd backend
python -m pip install -r requirements.txt
python -m pytest tests/
```

To run one test module:

```bash
python -m pytest tests/test_auth_dependencies.py
```

To run selected tests by name:

```bash
python -m pytest tests/ -k role_guard
```

For a concise summary:

```bash
python -m pytest tests/ -q
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

- `test_admin_mlflow.py`
- `test_artifact_storage.py`
- `test_auth_dependencies.py`
- `test_auth_logic.py`
- `test_diagnostic_session_audit.py`
- `test_mlflow_artifacts.py`
- `test_mlflow_cli.py`
- `test_mlflow_compare.py`
- `test_mlflow_e2e.py`
- `test_mlflow_handover.py`
- `test_mlflow_ledger_consumer.py`
- `test_mlflow_lifecycle.py`
- `test_mlflow_metric_batch.py`
- `test_mlflow_outbox.py`
- `test_mlflow_outbox_worker.py`
- `test_mlflow_professor.py`
- `test_mlflow_professor_api.py`
- `test_mlflow_professor_ledger.py`
- `test_mlflow_professor_postgres.py`
- `test_mlflow_professor_redaction.py`
- `test_mlflow_project_snapshot.py`
- `test_mlflow_queue_order.py`
- `test_mlflow_rebuild_preinit.py`
- `test_mlflow_routes.py`
- `test_mlflow_snapshot_storage_failures.py`
- `test_mlflow_worker.py`
- `test_mlflow_worker_docker.py`
- `test_steward_context.py`

## Important limitations

A passing test run establishes only that the collected tests passed in that environment. It does not establish production readiness or validate all concurrency and failure behaviour against real PostgreSQL transactions, S3-compatible storage, Docker, or GPU hardware. Tests that need external services must be run with those services configured; skipped tests should be reported as skipped, not as passed.

No test result is implied by this README. Record the actual command, environment, and result when the suite is run.
