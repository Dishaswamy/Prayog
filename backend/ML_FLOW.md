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

## Command-line client

All command-line tooling lives in the `cli/` package and is installed as `prayog` (see `cli/README.md` for every command). The former `backend/mlflow_cli.py` script was removed: creating projects, uploading and versioning a folder, running on the GPUs, reading results, downloading checkpoints, rebuilding and comparing are all `prayog` commands. The lower-level lifecycle endpoints (`/mlflow/experiments`, `/mlflow/runs/{id}/start|finish|metrics`, `/mlflow/snapshots/{id}/rebuilds`, ...) remain part of the HTTP API and are called directly, with the Prayog bearer token, by services such as the isolated worker below. The API stores and checks artifact bytes itself; lifecycle endpoints accept only server-issued artifact IDs.

## Worker-managed first-time execution

This is the older, DB-polling worker (`mlflow_worker.py`); GPU runs started with `prayog run` use the node agent instead (see "Versions, GPU runs and rebuilds"). Upload the code archive first, then start the run with the returned artifact ID:

```sh
curl -X POST "$PRAYOG_API_URL/mlflow/experiments/1/artifacts?kind=CODE" -H "Authorization: Bearer $PRAYOG_TOKEN" \
  -H "Content-Type: application/octet-stream" --data-binary @./code.zip
curl -X POST "$PRAYOG_API_URL/mlflow/runs/1/start" -H "Authorization: Bearer $PRAYOG_TOKEN" -H "Content-Type: application/json" \
  -d '{"code_artifact_id": "CODE_ARTIFACT_ID", "environment": {"entrypoint": "main.py", "python": "3.12"}}'
python mlflow_worker.py --once
curl "$PRAYOG_API_URL/mlflow/runs/1" -H "Authorization: Bearer $PRAYOG_TOKEN"
```

The worker validates the archive and checksum, claims the queued run atomically, runs its entrypoint in the isolated runtime image, stores bounded logs and any output weights, then finalizes the run and records a snapshot when artifact validation permits. A nonzero exit code, invalid archive, or timeout marks the first-time run failed; it never invokes Steward. When no `code_artifact_id` is given, `start` retains the caller-managed workflow, and the caller completes it with `POST /mlflow/runs/{id}/finish`. Do not call `finish` for a worker-queued run.
## Rebuild metric comparison

Compare a successful rebuild with its source run using per-metric absolute tolerances:

```sh
curl -X POST "$PRAYOG_API_URL/mlflow/rebuilds/12/compare-metrics" \
  -H "Authorization: Bearer $PRAYOG_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tolerances":{"loss":0.01,"accuracy":0.02}}'
```

The response reports each metric and step, expected and actual values, absolute difference, applied tolerance, and an overall pass/fail result. A missing metric fails comparison. Metrics without an explicit tolerance use zero tolerance. Comparisons are permitted only for successful rebuilds and recorded source snapshots; an empty comparison is not considered a pass.

## Lifecycle

1. Create a project using `POST /mlflow/projects`, then create a model in that project using `POST /mlflow/projects/{project_id}/models`. Project IDs and model IDs are separate, user-owned namespaces. A user's project names are unique, ignoring case and surrounding spaces; creating a second project with the same name returns `409 You already have a project named 'X'`.
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

## Versions, GPU runs and rebuilds

This is how a project goes from a folder on a user's disk to numbered GPU runs that can be reproduced. The user-facing commands are in `cli/README.md`; this section is the server's side.

### What is stored where

```
Project
 └─ ProjectVersion  A, B, C ...   one per distinct folder upload, identified by the BLAKE3 digest of its manifest
     └─ ModelRun    "A run 1", "A run 2" ...   numbered within the version; linked to its scheduler Job
         ├─ RunDatasetFile  path, size, BLAKE3 of each dataset file: kept for good, the bytes are not
         ├─ RunMetric       the training metrics
         ├─ RunCheckpoint   saved training states (step, best flag) -> WEIGHTS artifacts
         └─ ModelSnapshot   sealed when the run succeeds: version manifest + best checkpoint + settings
```

* **Code** is the version's manifest and its code chunks (see "Project snapshots with chunk reuse"). Every stored object is addressed by its BLAKE3 digest, so identical content is kept once, however many versions, runs or users name it.
* **Datasets are not kept.** To train on one, the user stages its files (`ml_staged_files`, per user, BLAKE3-addressed). A GPU node reads them for the run, and they are deleted `PRAYOG_STAGING_RETENTION_HOURS` (default 48) after the end of the last run that used them, or earlier on request. A file in use by an unfinished run is never deleted; a re-upload restarts the clock; an object is removed from storage only when no artifact and no other staged file names it. The run keeps each file's fingerprint (`ml_run_dataset_files`), so a later rebuild can insist on exactly the same data.
* **Two pointers per project**, as in a migration tool: *head* (the newest version) and *current* (`ml_projects.current_version_id`, what a run uses by default). Registering a version makes it current; `PUT /mlflow/projects/{id}/current` moves the pointer without changing any version. Registering identical content again finds the existing version rather than adding one.

### API (all under `/mlflow`, owner-scoped; another user's resources are "not found")

| | |
|---|---|
| `POST /projects/{id}/versions` | register a manifest artifact as the next version (idempotent by manifest hash); makes it current |
| `GET /projects/{id}/versions` | versions newest first, with head/current flags and run counts |
| `PUT /projects/{id}/current` | choose the current version (`{"version": "A"}`) |
| `POST /staging/missing` | which of these (BLAKE3, size) files must still be uploaded |
| `PUT /staging/files/{blake3}?size=N` | stage one file; the server recomputes the digest and refuses a mismatch |
| `POST /projects/{id}/runs` | queue a run (see below) |
| `GET /projects/{id}/runs[?version=A]`, `GET .../runs/{version}/{n}` | list / read runs |
| `GET /projects/{id}/runs/{version}/{n}/compare?tolerance=` | compare a rebuild with its original |
| `GET /projects/{id}/status` | current/head version, latest run, staged data held |
| `DELETE /projects/{id}/staged-data` | delete this project's finished-run data now |

`POST /projects/{id}/runs` takes `gpus`, `walltime` (HH:MM), `client_request_id`, and optionally `version`, `entrypoint`, `hyperparameters` and `dataset` (path, size, BLAKE3 per file, all already staged, else `409 DATASET_NOT_STAGED`). It submits an ordinary scheduler job (so every scheduling rule, access check and quota applies), creates the numbered run linked to it, and records the dataset fingerprint. The same `client_request_id` returns the same run.

### A rebuild

`rebuild_of: "A/1"` reproduces a finished run: the same version, entrypoint and hyperparameters (none may be given), and a dataset that must equal the original's recorded fingerprint exactly (`409 DATASET_MISMATCH` names the files that differ). The original must have a verified, sealed snapshot; anything wrong with it, including that it does not exist or belongs to someone else, is the same `404 REBUILD_REJECTED`. The rebuild is a normal job labelled `reproduces:<run id>`, with no priority advantage. `compare` reports each original metric against the rebuild's within an absolute tolerance, and whether the saved weights are the same bytes (equal BLAKE3 storage keys). **Metrics and weights can only match if the user's program is deterministic** (fixed seeds, deterministic kernels); otherwise the comparison reports the difference honestly.

### How the job ends reaches the run

The node agent reports through the scheduler. A release hook (`logic/mlflow_release.py`, registered by the API process and by the scheduler tick) follows the job inside the release transaction: running -> RUNNING; back in the queue after a platform stop -> QUEUED with the partial attempt's metrics and checkpoints discarded; completed -> SUCCEEDED and the snapshot is sealed (with a `RUN_SEALED` outbox event); failed or past its walltime -> FAILED with a reason; cancelled -> CANCELLED. Checkpoints and metrics of a failed or cancelled run are kept. A path with no release (a queued job cancelled) is caught by a sweep on the scheduler tick; the API also shows a finished job's outcome at once. The same tick deletes expired staged data.

### The node agent that executes runs

`python -m node_agent.agent` is the simulated agent plus real execution of allocations that carry a run (plain jobs behave as before):

```
python -m node_agent.agent --server https://prayog.example --units 4 --state-dir /var/lib/prayog-agent \
    --runner docker --image prayog-ml-runtime:python3.12 [--memory 16g --cpus 8] [--enrolment-token TOKEN]
```

1. acknowledges the start (a 409 means cancelled or requeued: nothing runs); 2. downloads the version's code chunks and the run's dataset through node-scoped endpoints (`/agent/allocations/{id}/workload|code|data`) and **verifies every byte** (chunk and archive SHA-256, each file against its inventory, each dataset file's BLAKE3, safe paths, no links), failing the run without starting it on any mismatch; 3. starts the program in a container with no network, a read-only root, no capabilities, a non-root user, process/memory/CPU limits and only the allocated GPUs (`--no-gpu-passthrough` for a node with no NVIDIA runtime); the code and data are read-only; 4. while it runs, sends new metrics and checkpoints (`/agent/allocations/{id}/metrics|results`); 5. at the end sends the rest and the log, and only then reports the exit, because the report releases the allocation and the server refuses later results. It enforces the walltime itself, stops the program when the server asks, and survives its own restarts (the started allocation is persisted before anything runs). Agent endpoints accept only a live allocation on the calling node.

**What the user's program sees.** It is started as `python <entrypoint>` in the code directory, with:

| | |
|---|---|
| `PRAYOG_HYPERPARAMETERS_JSON` | the run's hyperparameters |
| `PRAYOG_DATA_DIR` | the dataset, read-only, at the paths the user's dataset had |
| `PRAYOG_OUTPUT_DIR` | where to write results |

and writes `checkpoints/step-<N>.<ext>` (or `step-<N>-best.<ext>` for the best so far; write to `*.tmp` and rename, so a half-written file is never sent) and `metrics.jsonl` (one `{"name":..., "value":..., "step":...}` per line). stdout and stderr become the run's log. `--runner subprocess` runs the program with no isolation and is for a developer's own code on their own machine only.

### Configuration

| Variable | Default | |
|---|---|---|
| `PRAYOG_STAGING_RETENTION_HOURS` | 48 | how long staged data outlives the last run that used it |
| `PRAYOG_STAGING_MAX_FILE_BYTES` | 10 GiB | largest single staged file |

### Known limits

* A checkpoint or the log may be at most 250 MiB (the artifact limit); a larger checkpoint is skipped, not sent.
* The runtime image must already contain the program's dependencies: the node installs nothing and has no network. The environment recorded with a run is only its entrypoint, GPUs and walltime, not the runtime image's digest or its installed packages, so a rebuild on a different image is not detected.
* Staged data has a per-file limit but no per-user quota yet.
* Staging a file copies it once into the artifact store before it is linked, so a very large file briefly takes about twice its size on the server.

## Project snapshots with chunk reuse

The Prayog CLI (`cli/`, see `cli/README.md`) packages a project folder, including uncommitted edits: `prayog project link NAME FOLDER` uploads it the first time, `prayog project update` uploads a new version, and `prayog project pull NAME DESTINATION` restores a stored version.

The folder is inventoried by relative path with SHA-256 and byte size for every file, and files are labelled as lockfile, configuration, seed, data or source. Dataset files are fingerprinted but not uploaded; this avoids silently transferring large or sensitive datasets. Build caches, virtual environments, version-control metadata, result/artifact folders and likely secret files are excluded, and symbolic links are refused. The rest is written to a deterministic zip archive, divided into bounded chunks, and each chunk is uploaded as a CODE artifact unless the experiment already holds identical content (the server stores bytes under their BLAKE3 digest, so identical content is kept once). A manifest artifact records the ordered chunk IDs, digests, file inventory, archive digest and size; its artifact ID is the version ID.

`pull` downloads the manifest and chunks, verifies each chunk, the combined archive and every extracted file, and verifies dataset fingerprints against a local copy. Extraction rejects traversal paths, duplicate entries, symbolic links, special files, and destinations that already exist. A folder's versions are recorded locally in `.prayog/link.json`; the server does not yet keep a "current version" pointer for a project. A folder upload is distinct from the sealed ML Flow `ModelSnapshot` row.


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

