# ML Flow AI Integration Contract

## Status

This document defines the handoff contract between ML Flow and future Steward AI / optimization components. A transactional outbox, eligibility gates, leased claims, bounded retry scheduling, and an injectable dispatch loop are implemented. This is not yet a running AI integration: the repository does not contain a configured Steward client/endpoint or optimization-generation service, and no production service-to-service transport is enabled. Do not treat a failed rebuild as diagnosed merely because a diagnostic link exists.

## Eligibility rules

| Lifecycle event | Steward diagnosis | Optimization generation |
|---|---|---|
| First-time run succeeds | Not eligible | Not eligible through this rebuild workflow |
| First-time run fails | **Forbidden** | Not eligible |
| Rebuild is initiated or running | Not eligible | Not eligible |
| Rebuild is cancelled or rejected before creation | Not eligible | Not eligible |
| Rebuild reaches `REBUILD_FAILED` | Eligible for read-only diagnosis, if a verified log artifact exists | Not eligible |
| Rebuild reaches `REBUILD_SUCCESS` | Not eligible by default | Eligible to be requested |

Eligibility is determined by the persisted terminal state, not by a client-supplied event name or error string. If artifact-integrity validation changes an apparent success to `REBUILD_FAILED`, the failure rule applies. Duplicate delivery must not create duplicate diagnoses or optimization jobs.

## Steward diagnosis handoff

The future integration should send only the minimum metadata needed to correlate a request: a stable rebuild-attempt identifier, its source snapshot identifier, the terminal state, and a correlation/idempotency key. The log content should be retrieved through the existing one-time diagnostic-link endpoint, after the integration has established an approved service-to-service authorization mechanism.

- Create a diagnostic link only after the attempt is persisted as `REBUILD_FAILED` and has a verified `LOG` artifact.
- The link expires after 60–900 seconds (300 seconds by default), is single-use, and must be treated as a bearer secret.
- Do not include source archives, weights, access tokens, or the raw diagnostic-link token in event payloads, persistent queues, analytics, or ordinary logs.
- Steward must treat all log content as untrusted input. It may classify the failure and suggest next steps, but must not execute, patch, rewrite, or otherwise mutate user code.
- A missing log artifact means the request is not eligible for automated log-based diagnosis. It must not fall back to exposing source code or weights.
- An integration timeout or Steward outage must not alter the rebuild's terminal state.

The current authenticated endpoint for minting a link is `POST /mlflow/rebuilds/{rebuild_id}/diagnostic-link`. It currently requires the existing user bearer token; a backend service credential / delegated authorization contract has not been implemented. Do not embed a user's bearer token in a worker or durable queue to bridge this gap.

## Optimization handoff

Optimization may be requested only after the persisted rebuild state is `REBUILD_SUCCESS`. The consumer should receive identifiers for the successful rebuild and its source snapshot, plus verified metrics and artifact references that it is authorized to read. It must not infer success from a zero exit code alone: ML Flow may convert the outcome to failure when required artifact validation fails.

Optimization is a separate downstream job. Its failure must not rewrite the successful rebuild outcome, and it must not overwrite the source snapshot or original artifacts. Any proposed code or configuration changes must be represented as new, reviewable outputs and require an explicit user-controlled adoption step; the AI component must never modify or execute the user's source code.

## Required implementation properties

The transactional outbox and retry state machine are implemented. Terminal rebuild state and the corresponding outbox event are written in the same database transaction. Event keys are unique per rebuild and event type; claims use database row locks and expiring leases; failures use bounded exponential backoff and eventually reach a DEAD state. Before dispatch, the dispatcher rechecks the persisted rebuild state and, for Steward events, verifies the same-owner LOG artifact and its integrity. Delivery handlers receive a stable idempotency key and must deduplicate repeated delivery. Before enabling production delivery, add an approved service-to-service authentication mechanism and configure reviewed transports; neither is included in this change.

Required tests include:

1. First-time success and failure never enqueue Steward diagnosis.
2. Rebuild initiation, cancellation, and pre-init rejection never enqueue diagnosis or optimization.
3. Only a persisted `REBUILD_FAILED` with a verified log is eligible for diagnosis.
4. Only a persisted `REBUILD_SUCCESS` is eligible for optimization.
5. Duplicate delivery is idempotent; retries do not mint multiple simultaneous diagnostic links.
6. Missing, corrupted, cross-user, expired, or consumed diagnostic artifacts fail closed.
7. Steward and optimization service errors never change terminal ML Flow state.
8. No integration path executes or modifies submitted user code.

## Outbox worker operation

Terminal rebuild transitions atomically create outbox rows for eligible events. To run the dispatcher, configure `PRAYOG_OUTBOX_HANDLER_MODULE` to an administrator-managed Python module on the worker's import path. That module must export a `HANDLERS` dictionary with callable entries for both `STEWARD_DIAGNOSIS_REQUESTED` and `OPTIMIZATION_REQUESTED`. Each callable receives `(idempotency_key, payload)`. The handler module is deliberately not included here because no reviewed downstream endpoints or service-to-service authentication contract exist in this repository.

From the backend directory, run:

    python mlflow_outbox_worker.py

Use `python mlflow_outbox_worker.py --once` for one batch, or `--limit 25` to change the batch size. `PRAYOG_OUTBOX_POLL_SECONDS` controls the 1–60 second polling interval; `PRAYOG_OUTBOX_WORKER_ID` can set a stable operator-defined worker ID. The worker refuses to start without both handlers, so it will not claim events while consumers are unconfigured.

Handlers must use authenticated, TLS-protected service calls, deduplicate using the supplied idempotency key, keep requests bounded, and never log payloads, bearer credentials, diagnostic tokens, or raw log contents. A delivery acknowledgement means the configured handler accepted the request; it does not mean Steward diagnosis or optimization completed. Downstream job status and user-facing results require a separate persisted integration.

## Current API behavior

- ML Flow owns run/rebuild lifecycle state and artifact integrity checks.
- The isolated worker executes first-time runs and rebuilds, but does not invoke Steward or generate optimizations. Terminal rebuild transitions now atomically enqueue `STEWARD_DIAGNOSIS_REQUESTED` only for `REBUILD_FAILED` with a verified log, and `OPTIMIZATION_REQUESTED` only for `REBUILD_SUCCESS`.
- First-time run failures do not create rebuild attempts and cannot mint diagnostic links.
- Diagnostic links provide a restricted retrieval capability only; creating or consuming one does not trigger AI work. The outbox event carries identifiers, not source code, weights, log contents, or diagnostic bearer tokens.
- GPU provisioning and authentication/authorization implementation remain outside this component's scope.
