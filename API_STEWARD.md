# Prayog API: Steward

- **Base URL (local):** `http://127.0.0.1:8000` (the Steward is mounted on the platform API)
- **Interactive docs:** `/docs` lists every route below except the link page, `ui-config` and `healthz`.
- **Format:** JSON request and response bodies (`Content-Type: application/json`)

> **Scope:** this documents the endpoints of the **Steward**, Prayog's AI agent (`/steward/*`), and the interim rebuild routes it needs (`/rebuilds`). Authentication, roles, dates and the error format follow the [conventions in API.md](API.md#conventions); this file adds only what is specific to the Steward. How the Steward works and how to run it: [steward/DESIGN.md](steward/DESIGN.md).

The examples are real responses from the live development system (four processes on PostgreSQL; heuristic model adapter), shortened with `…`. The proposal examples show the shape only: the heuristic adapter cannot propose any field the platform can change, so the live data had no proposals.

---

## Contents

- [Conventions](#conventions)
- [Rebuilds (interim)](#rebuilds-interim)
- [S1: run diagnoses and proposals](#s1-run-diagnoses-and-proposals)
- [S3: rebuild-failure links and chat](#s3-rebuild-failure-links-and-chat)
- [S4: optimisation advice](#s4-optimisation-advice)
- [S2: hardware warnings (admin)](#s2-hardware-warnings-admin)
- [Knowledge base and health](#knowledge-base-and-health)
- [Endpoint summary](#endpoint-summary)

---

## Conventions

The platform's [conventions](API.md#conventions) apply: `Authorization: Bearer <token from /auth/login>`, JSON bodies, `detail` in every error. In addition:

| Topic | Rule |
|---|---|
| IDs | `run_id` is the platform's **run id** (`runs.id`, one execution of a job). `attempt_id` is a rebuild attempt id. Both are sent and returned as strings by `/steward`. Proposal and warning ids are UUIDs. |
| Visibility | Anything you may not see answers **404**, never 403, so ids cannot be probed. Admin routes answer 404 to non-admins. |
| Link and session errors | 403/404 bodies also carry `reason`: `not_found`, `wrong_state`, `no_session`, `expired`, `replayed`, `identity_mismatch`, `unknown_token`, `session_ended`, `session_invalid`. |
| Browser calls | With the `prayog_session` cookie instead of a bearer token, every `POST` must send `X-Steward-CSRF: 1`. |
| The Steward never writes | Its own service identity is refused (403) on every mutating route. Approving a proposal submits through the ordinary `submit_job()` path. |
| Timestamps | UTC with an explicit offset (`2026-10-14T20:49:21.083224+00:00`; link expiry uses `Z`). `/rebuilds` uses the platform's naive-UTC form. |
| Model outcome | `outcome` is `ok`, `refused`, `malformed` or `unavailable`. Anything but `ok` returns 200 with empty content and a `notices` entry; the platform keeps working when the model is down. |
| Citations | Every claim cites record ids from its own input (`log:…`, `run:…`, `led:…`, `ref:…`); uncited or out-of-scope claims are removed server-side and counted in `stripped_count`. |

---

## Rebuilds (interim)

Owned by the ledger sub-team; this interim version keeps S3 and S4 reachable until their snapshot store lands. A rebuild is an ordinary job under every scheduling rule, and its state follows the job's:

| Job state | Attempt `state` |
|---|---|
| IN_PROCESS, QUEUED, SCHEDULED, RUNNING | `REBUILD_INITIATED` |
| COMPLETED | `REBUILD_SUCCESS` |
| FAILED | `REBUILD_FAILED` |
| CANCELLED, WALLTIME_EXCEEDED | `REBUILD_CANCELLED` |

### POST /rebuilds
Rebuild a snapshot that one of your jobs ran.

```json
{ "snapshot_hash": "snap-v2", "gpus": 1, "walltime": "00:30", "client_request_id": "rb-001" }
```
`gpus` and `walltime` are validated as in `POST /jobs`. **201** with the attempt; a retry with the same `client_request_id` returns the same attempt with **200**.

```json
{ "attempt_id": 1, "snapshot_hash": "snap-v2", "job_id": 3, "job_state": "FAILED",
  "state": "REBUILD_FAILED", "created_at": "2026-10-14T20:49:55.055294" }
```
A snapshot none of your jobs ran is rejected without saying whether it exists, and no attempt is created:
```json
{ "detail": "Snapshot not found or not accessible.", "code": "REBUILD_REJECTED" }
```
(**404**)

### GET /rebuilds
Your attempts, newest first (same objects as above).

### GET /rebuilds/{attempt_id}
One of your attempts; 404 otherwise.

---

## S1: run diagnoses and proposals

When a run ends `FAILED`, the Steward diagnoses it in the background (Worker → Runtime). Nothing here blocks scheduling.

### GET /steward/runs/{run_id}/diagnoses
The run's owner or their supervising professor. Diagnoses are append-only, oldest first.

```json
[
  {
    "diagnosis_id": "71e96c42-0759-4066-bdfc-15009948d4ef",
    "run_id": "3",
    "outcome": "ok",
    "classification": "library_version",
    "claims": [
      { "text": "A library is missing or has an incompatible version.",
        "cites": [ { "kind": "log_excerpt", "record_id": "log:3:0" } ] }
    ],
    "matching_earlier_failures": [],
    "stripped_count": 0,
    "hardware_verdict": "unlikely_hardware",
    "hardware_indicators": [],
    "agent_version": "heuristic-1",
    "created_at": "2026-10-14T20:50:01.369852+00:00"
  }
]
```
- `classification`: `oom`, `missing_data`, `library_version`, `code_bug`, `interruption`, `hardware`, `unknown`.
- `matching_earlier_failures`: earlier failed runs of the same project that failed the same way (`earlier:<run_id>`).
- `hardware_verdict`: `likely_hardware`, `unlikely_hardware`, `inconclusive`; `hardware_indicators` come from a fixed list (for example `xid_error`).
- `[]` while the diagnosis is pending. **404** if the run is not yours (or your student's) or does not exist.

### GET /steward/proposals?state=PROPOSED|APPROVED|REJECTED
Your corrected-job proposals. A proposal changes only fields the platform can change (`gpu_count`, `walltime_minutes`).

| Field | Meaning |
|---|---|
| `proposal_id`, `run_id`, `diagnosis_id` | what it fixes and why |
| `state` | `PROPOSED`, `APPROVED`, `REJECTED` |
| `changes` | `[{field, new_value, reason: {text, cites}}]` |
| `current_spec`, `proposed_spec` | the job before and after |
| `requested_gpu_hours` | what approval would request |
| `decided_by`, `decided_at`, `submitted_job_id` | set on a decision |
| `submission_refusal` | why the last approval could not be queued (the proposal stays `PROPOSED`) |
| `created_at` | |

### GET /steward/proposals/{proposal_id}
One of your proposals; 404 otherwise.

### POST /steward/proposals/{proposal_id}/approve
Submits the proposed job through the normal request path: same validation, quota and ranking, a new submit time and no priority advantage. Idempotent per proposal.

```json
{ "state": "APPROVED", "submitted_job_id": "12", "refusal": null }
```
- **403** if your access is no longer active, or you may not submit to the project.
- **409** if the proposal was already decided.
- If the submission is refused, the response is `{"state": "PROPOSED", "submitted_job_id": null, "refusal": "<reason>"}`.

### POST /steward/proposals/{proposal_id}/reject
`{"state": "REJECTED", "submitted_job_id": null, "refusal": null}`. Nothing is queued. **409** if already decided.

---

## S3: rebuild-failure links and chat

### POST /steward/attempts/{attempt_id}/link
For the owner of a `REBUILD_FAILED` attempt (the CLI calls it when a rebuild fails). It returns a single-use link valid for 15 minutes; the token is stored only as a hash.

```json
{
  "attempt_id": "1",
  "url": "http://127.0.0.1:8000/steward/s/vIdtQIbl…",
  "expires_at": "2026-10-14T21:20:53.001713Z",
  "message": "Rebuild failed. Attempt ID: 1. To analyze this error with Steward, click here: http://127.0.0.1:8000/steward/s/vIdtQIbl…"
}
```
**403** `wrong_state` for any other attempt state, `no_session` without an unexpired platform token; **404** if it is not yours. An unused link stops working when the platform token that requested it expires (403 `session_ended`).

### GET /steward/s/{token}
The page the link opens. It has no side effects, so link previewers cannot use up the link. The page signs the user in through `/auth/login` if needed, then calls `/open`.

### POST /steward/s/{token}/open
Uses up the link. The caller must be the attempt's owner. This sets the `steward_s3` cookie (HttpOnly, SameSite=Strict, path `/steward`, 1 hour):

```json
{ "attempt_id": "1", "expires_at": "2026-10-14T22:05:53.045271+00:00" }
```
| Case | Response |
|---|---|
| another user | 403 `{"detail": "Access denied.", "reason": "identity_mismatch"}` |
| second use | 403 `{"detail": "Single-Use Violation: this link was already used.", "reason": "replayed"}` |
| after 15 minutes | 403, `reason: "expired"` |

### POST /steward/s3/messages
Needs the `steward_s3` cookie and the same user.

```json
{ "question": "Why did my rebuild fail?", "include_attempts": [] }
```
`question` is 1–2,000 characters. `include_attempts` lists up to 5 more of your own attempt ids to bring into context.

```json
{
  "outcome": "ok",
  "attempt_id": "1",
  "claims": [
    { "text": "A library is missing or has an incompatible version.",
      "provenance": "internal", "cites": ["log:3:0#0"] },
    { "text": "Your ledger records the environment this snapshot used.",
      "provenance": "internal", "cites": ["led:job-2#0"] }
  ],
  "steps": [
    { "explanation": "Recreate the environment from the snapshot's pinned versions.",
      "command": "pip install -r requirements.lock" }
  ],
  "evidence_found": true,
  "search": "disabled",
  "sources": {
    "log:3:0#0": { "kind": "log_excerpt", "url": null, "title": null },
    "led:job-2#0": { "kind": "ledger", "url": null, "title": null }
  },
  "notices": [],
  "cached": false
}
```
- `provenance` is one of:
  - `internal`: your own logs and ledger.
  - `literature`: reference papers, which carry a `url` and `title` in `sources`.
  - `external`: web results, never presented as fact.
  - `reasoning`: no evidence cited.
- `steps` are text to run yourself; the Steward runs nothing.
- `search` is `ok`, `unavailable` or `disabled`.
- `cached: true` means a repeated question was answered from this user's and attempt's cache.

### GET /steward/s3/session
The current session: `{attempt_id, expires_at, history: [{role: "user"|"steward", content, at}]}`.

### POST /steward/s3/logout
Revokes the session and clears the cookie: `{"ok": true}`. Otherwise the session lasts one hour.

---

## S4: optimisation advice

### POST /steward/attempts/{attempt_id}/optimize
For the owner of a `REBUILD_SUCCESS` attempt. The advice is advisory; nothing is changed or run.

```json
{
  "outcome": "ok",
  "attempt_id": "2",
  "suggestions": [
    { "kind": "training_setup",
      "text": "Consider the guidance in 'Large Batch Optimization for Deep Learning: Training BERT in 76 minutes' for this configuration.",
      "cites": ["ref:arxiv-1904.00962#3", "run:4"] }
  ],
  "notices": [
    "These suggestions are advisory. Nothing has been changed or run.",
    "Suggestions citing papers apply published findings; they are not measurements of your run."
  ],
  "sources": {
    "ref:arxiv-1904.00962#3": { "kind": "reference", "url": "https://arxiv.org/abs/1904.00962",
      "title": "Large Batch Optimization for Deep Learning: Training BERT in 76 minutes" },
    "run:4": { "kind": "run", "url": null, "title": null }
  }
}
```
(trimmed: three suggestions in the live response.) `kind` is `architecture`, `hyperparameter` or `training_setup`. **403** `wrong_state` for any other attempt state, for example:

```json
{ "detail": "prayog steward optimize requires REBUILD_SUCCESS; this attempt is REBUILD_FAILED.", "reason": "wrong_state" }
```

---

## S2: hardware warnings (admin)

Failures from two or more labs on one GPU within the window raise a warning. The Steward attaches an advisory finding. Admin views carry metadata only: no logs and no model text from users. These routes answer 404 to non-admins.

### GET /steward/admin/warnings?state=OPEN|RESOLVED|DISMISSED
```json
[
  {
    "warning_id": "0dead979-f197-4154-8261-2555d9fda351",
    "unit_signature": "n1/gpu0",
    "node_id": "n1",
    "state": "OPEN",
    "opened_at": "2026-10-14T20:51:15.943039+00:00",
    "closed_at": null, "closed_by": null, "close_reason": null,
    "distinct_labs": 2,
    "evidence_count": 5,
    "steward_finding": {
      "same_fault": true,
      "finding": { "verdict": "likely_hardware", "indicators": ["repeated_on_same_unit"] },
      "row_ids": ["ev:eeed8a95-…", "ev:635b256d-…"],
      "rationale": [ { "text": "4 failures from 2 labs on n1/gpu0", "cites": [ { "kind": "evidence", "record_id": "ev:eeed8a95-…" } ] } ]
    },
    "evidence": [],
    "allocation_history": []
  }
]
```
The list leaves `evidence` and `allocation_history` empty; the detail route fills them. `steward_finding` is `null` until the Steward has looked at the warning.

### GET /steward/admin/warnings/{warning_id}
As above, plus:
- `evidence`: each item is `{evidence_id, run_id, job_id, owner_lab, node_id, unit_id, at, exit_code, failure_category, hardware_verdict, hardware_indicators}`.
- `allocation_history`: who ran on those units recently.

### POST /steward/admin/warnings/{warning_id}/close
```json
{ "resolution": "RESOLVED", "reason": "Froze n1/gpu0; vendor ticket 4411" }
```
`RESOLVED` means the units were frozen or the node was drained. `DISMISSED` means the evidence does not point at hardware. The evidence is consumed, so the same failures do not raise the warning again. Response: `{"warning_id": "…", "state": "RESOLVED"}`. **409** if the warning is not open.

### GET /steward/admin/health
Aggregates only:
```json
{
  "runtime_reachable": true,
  "task_queue": { "DONE": 6 },
  "embedder": "hashing-384-v1",
  "search_provider": "none",
  "knowledge": { "embedder": "st:BAAI/bge-base-en-v1.5", "chunks": 299 },
  "rag": { "chunks": 7, "bytes": 1243, "sessions": 2 }
}
```
`rag` is present only when the maintenance database URL is configured. `runtime_reachable: false` means the Runtime is down: runs still seal and tasks wait.

---

## Knowledge base and health

### GET /steward/knowledge/documents
Any signed-in user. Lists the reference papers the Steward can cite: `[{source, title, authors, url, published, chunks}]`. The list is read-only over HTTP; see [steward/DESIGN.md](steward/DESIGN.md#6-knowledge-base-of-reference-papers) to add papers.

### GET /steward/ui-config, GET /steward/healthz
For the link page (`{"login_url": "/auth/login"}`) and for probes (`{"status": "ok"}`). No auth.

---

## Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/rebuilds` | any | rebuild one of your snapshots |
| GET | `/rebuilds` | any | your rebuild attempts |
| GET | `/rebuilds/{id}` | owner | one attempt |
| GET | `/steward/runs/{run_id}/diagnoses` | owner / their professor | S1 diagnoses of a run |
| GET | `/steward/proposals` | any | your proposals (`?state=`) |
| GET | `/steward/proposals/{id}` | owner | one proposal |
| POST | `/steward/proposals/{id}/approve` | owner, active access | queue the corrected job |
| POST | `/steward/proposals/{id}/reject` | owner | decline it |
| POST | `/steward/attempts/{id}/link` | owner, REBUILD_FAILED | single-use S3 link |
| GET | `/steward/s/{token}` | none | link page (no side effects) |
| POST | `/steward/s/{token}/open` | owner | use the link, start the session |
| GET | `/steward/s3/session` | session | session and history |
| POST | `/steward/s3/messages` | session | ask about the failed rebuild |
| POST | `/steward/s3/logout` | session | end the session |
| POST | `/steward/attempts/{id}/optimize` | owner, REBUILD_SUCCESS | S4 advice |
| GET | `/steward/admin/warnings` | ADMIN | hardware warnings (`?state=`) |
| GET | `/steward/admin/warnings/{id}` | ADMIN | one warning with evidence |
| POST | `/steward/admin/warnings/{id}/close` | ADMIN | resolve or dismiss |
| GET | `/steward/admin/health` | ADMIN | Steward health aggregates |
| GET | `/steward/knowledge/documents` | any | reference papers |
| GET | `/steward/ui-config`, `/steward/healthz` | none | page config, probe |
