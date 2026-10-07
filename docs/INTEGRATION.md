# Integrating the Steward into Prayog

This is for the sub-teams that own the API server, scheduler/Results Service, access and identity, the ledger and the CLI. It lists everything the Steward needs from you and everything it gives you. If you implement the nine ports and make the four calls below, the Steward works.

## 1. What you implement: the ports

All ports are Python `Protocol`s in [`prayog_steward/ports.py`](../src/prayog_steward/ports.py). In-memory reference implementations live in [`prayog_steward/fakes`](../src/prayog_steward/fakes/__init__.py); copy their semantics, which the tests depend on.

| Port | Owner | Methods | Notes |
|---|---|---|---|
| `RunReader` | GPU (Results Service) | `get_run`, `earlier_runs(project_id, before, limit)`, `telemetry` | `earlier_runs` must return **only that project's** runs, newest first (FR-GPU-148). The Steward re-checks anyway. |
| `LogStore` | GPU (content store) | `read_log(ref)` | Raw text. The Steward sanitises it before any use (ADR-S03). |
| `PrincipalResolver` | Access | `resolve(credential) -> Principal \| None` | Bearer token (CLI) or the `prayog_session` cookie value (web). Fill in `roles` (`platform_admin` for admins) and `session_id`. |
| `Authorizer` | Access | `authorization_active`, `may_submit` (DEP-03), `may_read_project` (owner or supervising Professor), `session_active` | `authorization_active=False` refuses proposal approval (ID-03). `session_active` lets Steward links die on logout (ID-12). |
| `RebuildAttempts` | Ledger | `get(attempt_id) -> AttemptView` | State as in §8.4; `run_id` is the attempt's run. |
| `SubmissionPort` | GPU (Request Service) | `submit(SubmissionRequest) -> SubmissionReceipt` | Same validation, quota, eligibility and ranking as a user submission, new submit time, **no priority advantage** (FR-GPU-150). Honour `idempotency_key` (the proposal ID) so a retried approval never queues twice. |
| `AuditSink` | Access/platform | `append(AuditEvent)` | The single append-only audit log (FR-INT-06). Events carry IDs only (FR-INT-07). |
| `AllocationHistory` | GPU | `for_units(unit_ids, since)` | For the admin's warning view (FR-GPU-158). |
| `LedgerHistory` | Ledger | `for_user(owner_id, project_id, limit)` | Must return only that owner's entries (NFR-SEC-002). Used by S3 and S4. |

Bundle them into `prayog_steward.container.Ports` with a zero-argument factory, e.g. `prayog/integration/steward.py:steward_ports`.

## 2. What you call

### a. When a run is sealed (Results Service, inside the seal transaction)

```python
from prayog_steward.hooks import on_run_sealed

with engine.begin() as conn:  # the transaction that seals the run
    ...  # your own sealing writes
    on_run_sealed(
        conn, run_view, settings=steward.settings, clock=steward.clock, log_tail=last_kb_of_stderr
    )  # log_tail optional (failure signature, ADR-S05)
```

This writes the failure event, per-unit fault evidence and the S1 task, and evaluates the hardware-warning threshold, all atomically with your seal. It never calls the model and never blocks on the Steward (NFR-GPU-055). It is idempotent per run.

### b. Mount the API (API server)

```python
from prayog_steward.api.app import build_router, install_error_handlers, install_security_headers
from prayog_steward.container import build
from prayog_steward.config import StewardSettings

steward = build(StewardSettings(), steward_ports())
app.include_router(build_router(steward))  # everything under /steward
install_error_handlers(app)  # 404/403/409 mapping, no stack traces
install_security_headers(app)  # CSP, no-referrer (link tokens), no-store
app.mount("/steward/ui", StaticFiles(directory=prayog_steward.api.app.UI_DIR, html=True))
```

Or run it standalone: `python -m prayog_steward.entrypoints api --ports prayog.integration.steward:steward_ports`.

### c. Run the Worker (one or more processes)

```bash
python -m prayog_steward.entrypoints worker --ports prayog.integration.steward:steward_ports
```

Any number of workers is safe (`FOR UPDATE SKIP LOCKED` plus leases). With `STEWARD_MAINTENANCE_DATABASE_URL` set, the worker also runs the hourly retention purge (NFR-SEC-008).

### d. CLI

```python
from prayog_steward.cli import app as steward_cli, StewardClient, report_rebuild_failure

cli.add_typer(
    steward_cli, name="steward"
)  # prayog steward link|optimize|diagnosis|proposals|approve|reject

# when `prayog run --rebuild <snap>` ends REBUILD_FAILED:
print(report_rebuild_failure(StewardClient(api_url, token), attempt_id, snapshot_id, reason))
```

### e. On account deactivation (Access)

```python
from prayog_steward.retention import purge_user

purge_user(
    steward.engine, user_id
)  # personal Steward state only (A35); diagnoses and projects stay
```

## 3. Database

- PostgreSQL 15+. **No extensions** (ADR-S09).
- Our tables live in schemas `steward` and `steward_private`. The Alembic version table is `steward.alembic_version`, so it never collides with yours.
- Migrate as the schema owner: `STEWARD_DATABASE_URL=<owner url> STEWARD_APP_ROLE=prayog_app alembic upgrade head`. `STEWARD_APP_ROLE` grants the application role what it needs.
- **The application role must not be a superuser and must not have BYPASSRLS.** Both bypass row-level security, which is what isolates users' diagnostic data (NFR-SEC-002). `tests/integration/test_schema.py::test_app_role_cannot_bypass_rls` shows how to check.
- Optional maintenance role with `BYPASSRLS`, used only for the retention purge and the admin health aggregates: `STEWARD_MAINTENANCE_DATABASE_URL`.

## 4. Configuration

Every setting is an environment variable prefixed `STEWARD_`; see [`config.py`](../src/prayog_steward/config.py) and `.env.example`. The spec's parameters (§16.2, §16.4) have the spec's defaults and invariants. The Runtime is configured separately: `STEWARD_REASONING=claude|heuristic|stub`, `STEWARD_MODEL`, `STEWARD_EFFORT`, `ANTHROPIC_API_KEY`.

## 5. Processes

| Process | Command | Needs |
|---|---|---|
| Runtime | `uvicorn steward_runtime.app:app --port 8100` | Anthropic API key. **No** database credentials (ADR-S01). |
| API | `prayog_steward.entrypoints api` (or mounted) | DB app role, `STEWARD_RUNTIME_URL` |
| Worker | `prayog_steward.entrypoints worker` | DB app role, `STEWARD_RUNTIME_URL`, optional maintenance URL |

Steward inference runs on the API/Worker/Runtime hosts' CPUs and calls the Claude API. It never uses the scheduled GPU pool (NFR-PERF-001).

## 6. Things the other teams should know

- **CR-S1 (ADR-S05):** evidence rows carry an optional sanitised `failure_signature`. Please confirm it is acceptable under NFR-GPU-056.
- **Browser sign-in (CR-H):** the Steward reads the platform session from the `prayog_session` cookie. If your cookie has another name, change `PLATFORM_COOKIE` in `api/app.py`. Mutating requests authenticated by cookie must send `X-Steward-CSRF: 1`.
- **Rebuild attempts:** the Steward never changes attempt state; link use is a security event only (§8.4).
- **Open items** with the defaults we chose are in [ADR-S08](decisions/ADR-S08-open-items-and-defaults.md).
