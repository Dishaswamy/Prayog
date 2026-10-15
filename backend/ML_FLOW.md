# ML Flow lifecycle API

Every endpoint requires the existing Prayog bearer token. Projects and models are separate user-owned namespaces. Each experiment belongs to one project and one model; every execution run, snapshot, artifact and rebuild attempt carries or inherits that same Project ID and Model ID. All records are scoped to the authenticated database user. A resource owned by another user is reported as not found.

## Setup and validation

From `backend`, install `requirements.txt`, configure the existing database and `JWT_SECRET_KEY` environment variables, then run:

```sh
alembic upgrade head
uvicorn main:app --reload
pytest -q
```

Set `PRAYOG_ARTIFACT_ROOT` to a private local cache/staging directory writable by the API and worker processes. The default is `backend/artifacts`. Artifact files are never exposed as static files. Uploads are limited to 250 MiB per artifact. `PRAYOG_ARTIFACT_STORAGE=local` (default) stores objects on local disk; all API and worker instances must share that private durable volume. For durable S3-compatible storage, set `PRAYOG_ARTIFACT_STORAGE=s3`, `PRAYOG_S3_BUCKET`, and optionally `PRAYOG_S3_PREFIX` (default `prayog-artifacts`), `PRAYOG_S3_REGION` / `AWS_DEFAULT_REGION`, `PRAYOG_S3_ENDPOINT_URL` for compatible providers, and `PRAYOG_S3_SERVER_SIDE_ENCRYPTION` (default `AES256`). Credentials are loaded by the standard AWS SDK credential chain (environment, workload role, or configured profile); do not store credentials in source control. The bucket must be private, block public access, enforce TLS, and restrict read/write/delete access to the API and worker identities. In S3 mode, `PRAYOG_ARTIFACT_ROOT` is a local cache/staging path, not the source of truth; each API and worker must have enough private disk space for the largest artifact being processed. The secure, expiring diagnostic-link API is described below; links are minted only for verified logs from failed rebuild attempts. After configuring local or S3-compatible storage, run `python mlflow_storage_smoke.py` from `backend` to verify upload, download/checksum, and cleanup using a temporary random key; this smoke test does not touch ML Flow database records.

## CLI client

Set `PRAYOG_API_URL` and `PRAYOG_TOKEN`, then run commands from `backend` with `python mlflow_cli.py --help`. The client supports `create-project`, `create-model`, `create-experiment`, `create-run`, `start-run` (including worker-managed execution via `--code-artifact-id` and optional `--weights-artifact-id`), `upload-artifact`, `download-artifact`, `list-snapshots`, `snapshot`, `run-status`, `metric`, `finish-run`, `rebuild`, `rebuild-status`, and `finish-rebuild`. It uploads local files as artifact bytes and passes only server-issued artifact IDs to lifecycle endpoints. Keep the token in the environment rather than the shell command history.

The CLI is an API client, not a sandbox or execution worker. The rebuild command records a rebuild request and returns the new attempt/execution IDs. When `start-run` receives a verified `--code-artifact-id`, it moves the first-time run to `QUEUED`; the separate `mlflow_worker.py` process claims it and moves it to `RUNNING`. The worker consumes queued first-time runs and rebuild attempts and executes the archived project in a restricted Docker container. It does not run user code in the API process.

Example first-time run (replace the IDs and file paths with the values returned by your API):

```sh
export PRAYOG_API_URL=http://localhost:8000
export PRAYOG_TOKEN="paste-your-existing-bearer-token-here"
python mlflow_cli.py create-project --name "research-project"
python mlflow_cli.py create-model --project-id "PROJECT_ID_FROM_RESPONSE" --name "baseline-model"
python mlflow_cli.py create-experiment --project-id "PROJECT_ID_FROM_RESPONSE" --model-id "MODEL_ID_FROM_RESPONSE" --name "baseline-training"
python mlflow_cli.py create-run --experiment-id 1 --hyperparameters-json '{"learning_rate": 0.001}' --environment-json '{"python": "3.12"}'
python mlflow_cli.py start-run --run-id 1
python mlflow_cli.py metric --run-id 1 --name loss --value 0.42 --step 1
python mlflow_cli.py finish-run --run-id 1 --experiment-id 1 --status SUCCEEDED --code-file ./code.zip --weights-file ./weights.bin
```

The example assumes the server returned experiment ID `1` and run ID `1`; always use the actual IDs returned by the API. For a rebuild, run `python mlflow_cli.py rebuild --snapshot-id <SNAPSHOT_ID>`, then have the execution worker report the outcome with `finish-rebuild`.

## Worker-managed first-time execution

Upload the code archive before starting the run. Use the artifact ID returned by the upload command:

```sh
python mlflow_cli.py upload-artifact --experiment-id 1 --kind CODE --file ./code.zip
python mlflow_cli.py start-run --run-id 1 --code-artifact-id CODE_ARTIFACT_ID --environment-json '{"entrypoint":"main.py","python":"3.12"}'
python mlflow_worker.py --once
python mlflow_cli.py run-status --run-id 1
```

The worker validates the archive and checksum, claims the queued run atomically, runs its entrypoint in the isolated runtime image, stores bounded logs and any output weights, then finalizes the run and records a snapshot when artifact validation permits. A nonzero exit code, invalid archive, or timeout marks the first-time run failed; it never invokes Steward. When `--code-artifact-id` is omitted, `start-run` retains the caller-managed workflow shown above, and the caller completes it with `finish-run`. Do not call `finish-run` for a worker-queued run.
## Lifecycle

1. Create a project using `POST /mlflow/projects`, then create a model in that project using `POST /mlflow/projects/{project_id}/models`. Project IDs and model IDs are separate, user-owned namespaces.
2. Create an experiment using `POST /mlflow/experiments` with both `project_id` and `model_id`. The server verifies that the model belongs to the authenticated user's project.
3. Create a run using `POST /mlflow/experiments/{experiment_id}/runs`.
4. Start it with `POST /mlflow/runs/{run_id}/start`.
5. Upload the code archive with `POST /mlflow/experiments/{experiment_id}/artifacts?kind=CODE`, sending the archive as the raw request body. Upload optional trained weights with `kind=WEIGHTS` and logs with `kind=LOG`. Use the returned artifact IDs; the server computes SHA-256 and byte size.
6. Log metrics while the run is `RUNNING` using `POST /mlflow/runs/{run_id}/metrics`, or send up to 500 metrics atomically with `POST /mlflow/runs/{run_id}/metrics/batch` using `{"metrics":[{"name":"loss","value":0.7,"step":1}]}`. Each `(name, step)` pair can occur only once per run; duplicate pairs in a batch are rejected before writing, and a database uniqueness conflict rolls back the whole batch.
7. Log any metrics declared in the run environment's optional `expected_metrics` list while the run is `RUNNING`. Finish using `POST /mlflow/runs/{run_id}/finish` with `status` equal to `SUCCEEDED` or `FAILED`, and any available artifact IDs. Execution completion is independent of snapshot completeness: the terminal run is preserved even if artifact validation or expected-metric logging is incomplete. The server always records a queryable snapshot; it is `RECORDED` only when the required code artifact, every supplied optional artifact, and every expected metric are present and verified. Otherwise it is `INCOMPLETE` with `completeness_reasons`. Incomplete snapshots remain inspectable but cannot be rebuilt.
8. List snapshots using `GET /mlflow/experiments/{experiment_id}/snapshots` and retrieve one with `GET /mlflow/snapshots/{snapshot_id}`.
9. Start a rebuild with `POST /mlflow/snapshots/{snapshot_id}/rebuilds`. Before creating an attempt ID, the server verifies ownership, artifact existence and artifact checksums. A valid request creates a new `REBUILD_INITIATED` attempt and a linked `REBUILD` execution run.
10. Complete the attempt using `PATCH /mlflow/rebuilds/{rebuild_id}`. Send `REBUILD_SUCCESS`, `REBUILD_FAILED`, or `REBUILD_CANCELLED`. A failure requires a summary. Optional logs and output weights from successful or failed attempts must reference uploaded artifacts. If a required artifact becomes unavailable after initialization, the attempt is finalized as failed rather than falsely reported successful. Terminal attempts are immutable; a retry creates a new attempt and execution ID.
11. Inspect an attempt with `GET /mlflow/rebuilds/{rebuild_id}`. Download an owned artifact using `GET /mlflow/artifacts/{artifact_id}/content`. The API rechecks its digest before returning the bytes.

## Secure diagnostic links

A user may mint a short-lived, one-time capability link only for a rebuild already in `REBUILD_FAILED` with a verified LOG artifact. The link lifetime defaults to 300 seconds and is restricted to 60–900 seconds. The database stores only a SHA-256 hash of the random token, plus the owner, failed rebuild, log artifact, expiry and consumption timestamp.

Create a link with the existing bearer token:

```sh
curl -X POST "$PRAYOG_API_URL/mlflow/rebuilds/REBUILD_ID/diagnostic-link" \
  -H "Authorization: Bearer $PRAYOG_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ttl_seconds":300}'
```

The response includes a relative `path` and `expires_at`. Append the path to the configured API base URL when handing it to an authorized diagnostic consumer. The unauthenticated GET succeeds once, before expiry; subsequent, expired, malformed, or unknown tokens return not found. Responses are non-cacheable and set `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`. The link exposes only the failed rebuild's verified log artifact, not source code or weights. Do not put links in public tickets or analytics, and avoid logging token-bearing paths. A consumer must treat the URL as a bearer secret.

This capability is a retrieval mechanism, not a Steward integration: no diagnosis is triggered by creating or consuming the link. First-time run failures cannot mint diagnostic links.

## State rules

- Caller-managed first-time execution: `CREATED -> RUNNING -> SUCCEEDED | FAILED`. Worker-managed first-time execution: `CREATED -> QUEUED -> RUNNING -> SUCCEEDED | FAILED`. First-time failures do not invoke Steward.
- Run logging state is `COMPLETE` or `DEGRADED`. A degraded run preserves its terminal execution status and returns non-sensitive `logging_warnings`; absence of a valid snapshot is never represented as `RECORDED`.
- Snapshot: `RECORDED` only when code, every supplied optional artifact, and every declared expected metric are complete and verified. Otherwise a queryable `INCOMPLETE` snapshot stores non-sensitive completeness reasons. Incomplete snapshots are immutable and cannot be rebuilt or promoted. Snapshot metadata is immutable; logging problems set the run's `logging_state=DEGRADED`.
- Pre-init validation failures return `REBUILD_REJECTED` in the error detail and create no attempt ID. Unauthorized or unknown snapshot IDs both return not found.
- Rebuild attempt: `REBUILD_INITIATED -> REBUILD_SUCCESS | REBUILD_FAILED | REBUILD_CANCELLED`.
- A rebuild attempt has its own linked execution run. Failed and successful rebuilds are retained in history. Cancellation is terminal and does not create a terminal snapshot or register final weights.
- Metrics can only be recorded while an execution is running. Duplicate metric names at the same step are rejected. The CLI/API caller should treat metric-storage errors as logging degradation, not as a reason to terminate the underlying experiment.
- Steward is not invoked by this API. A future CLI integration may offer failure diagnosis only after `REBUILD_FAILED`; optimization must be gated on `REBUILD_SUCCESS`.

## Isolated rebuild worker

Build the default, locally available runtime image from the repository root:

    docker build -t prayog-ml-runtime:python3.12 -f backend/worker/Dockerfile backend/worker

Run the worker as a separate process from the API server, from the backend directory, with the same database and artifact-storage configuration. In local mode, the API and worker must share the same private artifact volume; in S3 mode, both need the same private bucket configuration and authorized credentials:

    python mlflow_worker.py

Use python mlflow_worker.py --once to process at most one queued rebuild and exit. Configure PRAYOG_WORKER_IMAGE to select an administrator-managed, prebuilt runtime image and PRAYOG_WORKER_TIMEOUT_SECONDS to set the execution timeout (1–1800 seconds). The configured image must already exist locally; the worker never pulls images while processing a user job.

The worker executes first-time runs that are started with a verified code artifact, as well as rebuilds from snapshots. A code artifact must be a ZIP archive with a Python entrypoint; by default the entrypoint is main.py, or it may be set to a safe relative .py path in the rebuild environment. The source tree is mounted read-only at /workspace. If the snapshot contains weights, they are mounted read-only at /input/weights.bin. The project may write an optional output artifact to /output/weights.bin. Hyperparameters are available as JSON in PRAYOG_HYPERPARAMETERS_JSON.

Execution containers have no network, no added Linux capabilities, a read-only root filesystem, a non-root user, process/memory/CPU limits, a bounded temporary filesystem, and bounded captured logs. Only the source archive, optional input weights, and non-secret run metadata are passed into the container. The worker validates ZIP paths, entrypoint paths, artifact digests, and output-file types before recording results. A crashed worker's stale claimed rebuilds are marked failed after the execution timeout plus a recovery grace period; they are not silently left running. The API server itself never launches the submitted project.

The default runtime image contains Python only. Do not install arbitrary project requirements inside a job or permit dependency installation from the public internet. Add reviewed dependencies to an administrator-managed runtime image; production images should pin the base image by digest and be scanned before deployment. The worker requires access to a Docker daemon, so run it as a trusted platform service; never mount the Docker socket into user execution containers.

## Scope and remaining integration work

The worker now supports first-time run dispatch through the verified code-artifact ID supplied at start time, alongside snapshot rebuilds. It does not install arbitrary dependencies, provision GPUs, or restore a fully captured operating-system image. S3-compatible artifact storage, secure expiring one-time diagnostic log links, and a transactional outbox for eligible terminal rebuild events are implemented; deployment-specific bucket configuration and real AI consumers remain follow-up work. Steward RAG, post-success optimization generation, frontend progress integration, and professor enrollment/RBAC also remain follow-up work. The safety gates and downstream interface are documented in [AI_INTEGRATION.md](AI_INTEGRATION.md): Steward diagnosis is enqueued only after a persisted rebuild failure with a verified log; it must never be invoked for a first-time run failure, modify user code, or execute user code. Optimization is enqueued only after a persisted successful rebuild. Outbox delivery has leased claims, persisted-state revalidation, bounded retries, and dead-letter status, but the production transport, service-to-service authorization, and real downstream consumers are not configured. GPU provisioning and authentication/authorization remain outside this worker's scope.

## Project snapshots with chunk reuse

The CLI can package the current working tree, including uncommitted edits, with `snapshot-project --experiment-id ID --project-dir PATH`. It inventories eligible files by relative path, records SHA-256 and byte size for every file, and labels lockfiles, configuration, seeds, data, and source files. Dataset files are fingerprinted but not uploaded or included in the materialized archive; this avoids silently transferring large or sensitive datasets. Common build caches, virtual environments, version-control metadata, result/artifact folders, and likely secret files are excluded; symbolic links are refused so the archive cannot silently include files outside the project tree.

The CLI creates a deterministic archive, divides it into bounded chunks, and checks the experiment's existing artifact metadata before uploading each chunk. Matching content-addressed chunks are reused. A separate manifest artifact records the ordered chunk IDs, digests, file inventory, archive digest, and size. Use `materialize-project-snapshot --experiment-id ID --manifest-artifact-id ID --output-dir PATH --data-dir PATH` to download the manifest and chunks, verify each chunk and the combined archive, verify every extracted file against the inventory, and verify external dataset fingerprints against a local data root with the same relative paths. If the manifest contains data fingerprints, `--data-dir` is required. Extraction rejects traversal paths, duplicate entries, symbolic links, special files, and output directories that already exist.

Example:

```bash
python mlflow_cli.py snapshot-project --experiment-id 12 --project-dir .
python mlflow_cli.py materialize-project-snapshot --experiment-id 12 --manifest-artifact-id ARTIFACT_ID --output-dir ./restored-project --data-dir .
```

The returned `manifest_artifact_id` identifies the manifest; the manifest references the chunk artifact IDs. Keep the manifest ID with the project record. This project snapshot bundle is distinct from the sealed ML Flow `ModelSnapshot` row: materialize it and, when recording a model run, upload the resulting code archive through the normal run lifecycle.


## Administrator ML Flow overview

Administrators can request `GET /admin/mlflow/overview` with an existing Prayog bearer token. Authorization uses the authenticated database user's `role` and requires `ADMIN`; a role claim supplied only by a client is not trusted. The response contains aggregate project/model/experiment/run/snapshot/artifact/metric/rebuild/outbox counts, run and rebuild states, and logical artifact metadata bytes. It intentionally excludes source code, hyperparameters, dataset metadata, descriptions, artifact storage keys, and artifact contents.

The artifact storage backend field reports the configured `PRAYOG_ARTIFACT_STORAGE` setting only; it is not a live connectivity check. `logical_artifact_bytes` sums artifact metadata sizes and can exceed physical object storage usage when identical bytes are deduplicated.


## Rebuild pre-initiation rejection boundary

A rebuild request is rejected before creating an execution run or Attempt ID unless the authenticated user owns a `RECORDED` snapshot and all required source artifacts pass integrity verification. Missing, foreign-owned, incomplete, and corrupt/unavailable snapshots return the same generic `REBUILD_REJECTED: snapshot could not be validated` response. The response does not distinguish whether a snapshot exists, and pre-initiation rejection does not enqueue an outbox event or invoke Steward. Once an attempt has been created, subsequent execution failures retain that Attempt ID and follow the normal terminal failure path.


## Transactional outbox and experiment ledger

A successfully recorded snapshot creates a `RUN_SEALED` outbox event in the same database transaction as the run and snapshot state. The dispatcher revalidates the persisted run and recorded snapshot before delivery. The experiment-ledger consumer stores event metadata under a stable `event_key`; the database uniqueness constraint makes repeated delivery idempotent. Event payloads contain identifiers and the code digest, not artifact contents or secrets. Existing rebuild terminal events retain their leased dispatch, bounded retries, and dead-letter behavior.


## Project IP handover

The Push & Approve workflow is available through the ML Flow API:

- `POST /mlflow/projects/{project_id}/handover` with `{"professor_id": 123}` creates a `HANDOVER_PENDING` request. The target must be the authenticated owner's assigned supervising Professor.
- `GET /mlflow/handovers/{handover_id}` returns the request to its owner, target Professor, or an administrator.
- `POST /mlflow/handovers/{handover_id}/approve` is restricted to the target Professor and verifies every inventoried artifact's persisted bytes and SHA-256 before changing ownership.
- `POST /mlflow/handovers/{handover_id}/reject` records a terminal rejection; `POST /mlflow/handovers/{handover_id}/cancel` lets the source owner cancel a pending request. Terminal requests cannot be approved or changed.

Approval changes project and completed-record ownership in one database transaction. Artifact bytes are not copied because their storage keys remain stable; each inventoried artifact is read and checksum-verified before the transaction. If verification fails, the request stays pending, ownership remains unchanged, and the failed artifact IDs are recorded.


## Professor experiment ledger

Authenticated Professors can query `GET /mlflow/professor/ledger` to see sealed experiment records for currently assigned students. The query is scoped to active approved student relationships and only returns run/snapshot identifiers and digest metadata for persisted `RECORDED` snapshots. It excludes source code, weights, hyperparameters, dataset metadata, logs, Steward sessions, and retrieval state. Non-Professor callers are rejected.


## Professor read-only ML Flow views

The `/mlflow/professor` endpoints expose projects, models, experiments, runs, snapshots, rebuilds, metrics, and artifact metadata only for currently assigned students/researchers with active approved relationships. Authorization is checked against database relationships, not client-supplied role claims. Unrelated resources are treated as not found. Responses omit artifact bytes, source code, weights, hyperparameters, and private run configuration.


## Sealed-run ledger delivery

Completed runs enqueue a `RUN_SEALED` outbox event only when snapshot verification produces a `RECORDED` snapshot. The outbox worker handles this event with the built-in ledger consumer; its unique event key makes retries idempotent. Steward diagnosis and optimization remain administrator-configured handlers and are still required before the worker starts, so missing AI integrations cannot silently consume those events.

