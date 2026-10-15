# Prayog API: GPU Scheduling and Execution

- **Base URL (local):** `http://127.0.0.1:8000`
- **Interactive docs:** `/docs` (Swagger UI), `/redoc`
- **Format:** JSON request and response bodies (`Content-Type: application/json`), except `/metrics` (plain text)

> **Scope:** this documents the endpoints of the **GPU scheduling and execution** part of the backend: submitting and managing GPU jobs (`/jobs/*`), the Platform Admin's operations on the GPU pool (`/admin/*`), the node agent's side of dispatch (`/agent/*`) and the scheduler metrics (`/metrics`). Authentication, roles, dates and the error format follow the [conventions in API.md](API.md#conventions); this file adds only what is specific to scheduling.

---

## Contents

- [Conventions](#conventions)
- [How a job runs](#how-a-job-runs)
- [Jobs](#jobs)
  - [POST /jobs](#post-jobs)
  - [GET /jobs/{id}](#get-jobsid)
  - [PATCH /jobs/{id}](#patch-jobsid)
  - [POST /jobs/{id}/cancel](#post-jobsidcancel)
  - [GET /jobs/quota](#get-jobsquota)
- [GPU nodes (admin)](#gpu-nodes-admin)
  - [POST /admin/nodes](#post-adminnodes)
  - [GET /admin/nodes](#get-adminnodes)
  - [POST /admin/nodes/{id}/drain](#post-adminnodesiddrain)
  - [POST /admin/nodes/{id}/return](#post-adminnodesidreturn)
  - [POST /admin/nodes/{id}/decommission](#post-adminnodesiddecommission)
  - [POST /admin/nodes/{id}/escalate](#post-adminnodesidescalate)
  - [POST /admin/nodes/{id}/abort-decommission](#post-adminnodesidabort-decommission)
  - [POST /admin/nodes/{id}/unit-count](#post-adminnodesidunit-count)
  - [GET /admin/alerts](#get-adminalerts)
  - [POST /admin/alerts/{id}/acknowledge](#post-adminalertsidacknowledge)
- [Maintenance windows (admin)](#maintenance-windows-admin)
  - [POST /admin/maintenance/preview](#post-adminmaintenancepreview)
  - [POST /admin/maintenance](#post-adminmaintenance)
  - [GET /admin/maintenance](#get-adminmaintenance)
  - [PATCH /admin/maintenance/{booking_id}](#patch-adminmaintenancebooking_id)
  - [DELETE /admin/maintenance/{booking_id}](#delete-adminmaintenancebooking_id)
- [Node agent](#node-agent)
  - [POST /agent/enrol](#post-agentenrol)
  - [POST /agent/heartbeat](#post-agentheartbeat)
  - [GET /agent/allocations](#get-agentallocations)
  - [POST /agent/allocations/{id}/acknowledge](#post-agentallocationsidacknowledge)
  - [POST /agent/allocations/{id}/report](#post-agentallocationsidreport)
- [Metrics](#metrics)
  - [GET /metrics](#get-metrics)
- [Objects](#objects)
- [Endpoint summary](#endpoint-summary)
- [Running it locally](#running-it-locally)

---

## Conventions

### Authentication
- **Users and admins:** `Authorization: Bearer <access_token>` from `/auth/login`, as in [API.md](API.md#authentication).
- **Node agents:** `X-Node-Credential: <credential>`, obtained once from [POST /agent/enrol](#post-agentenrol). A node only ever sees and changes its own allocations.

### Lab and account type
Each user acts for one lab with an account type, `research` or `course` (a course account's "lab" is its sponsorship group). The server reads both from its own records; the client never sends them. Until the authorization record carries them, they come from the `scheduling_contexts` table (see [Running it locally](#running-it-locally)). A user with no lab gets `AUTHORIZATION_INACTIVE` when submitting.

### Rejection codes
A refused job request or edit carries a machine-readable `code` beside `detail`, and the message names the rule and its limit:

```json
{ "detail": "Requested 10 GPUs, but the largest node you can use has 8.", "code": "GPUS_EXCEED_MAX_NODE" }
```

| Code | Status | When |
|---|---|---|
| `INVALID_GPUS` | 400 | `gpus` is not a positive whole number |
| `INVALID_WALLTIME` | 400 | `walltime` is not `HH:MM`, or is under the minimum (`00:01`) |
| `WALLTIME_EXCEEDS_MAX` | 400 | walltime over the maximum (`48:00` by default) |
| `GPUS_EXCEED_MAX_NODE` | 400 | more GPUs than the largest node your account could ever use: teaching-pool nodes for course accounts; shared and own-lab nodes for research |
| `QUOTA_EXCEEDED` | 400 | a course request over the sponsorship's remaining GPU-hour cap |
| `QUEUE_LIMIT_REACHED` | 400 | you already have the maximum number of active jobs (50 by default) |
| `AUTHORIZATION_INACTIVE` | 403 | you have no GPU authorization (no lab and account type) |
| `JOB_NOT_QUEUED` | 409 | an edit of a job that is no longer `QUEUED` |
| `MAINTENANCE_COLLISION` | 409 | a maintenance window would stop running jobs and was not confirmed |

A rejected request is never stored and never queued.

### Enum values
| Name | Values |
|---|---|
| Job state | `IN_PROCESS`, `QUEUED`, `SCHEDULED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `WALLTIME_EXCEEDED`, `REJECTED` |
| Queue reason | `NO_ELIGIBLE_NODE`, `INSUFFICIENT_FREE_UNITS`, `MAINTENANCE_WINDOW`, `LOST_CLAIM_RACE`, `OUTRANKED`, `OWNER_CLAIM_PENDING`; after a platform stop also `NODE_FAILURE`, `DISPATCH_TIMEOUT`, `MAINTENANCE`, `EMERGENCY_DRAIN` until the next pass |
| Run end state | `COMPLETED`, `FAILED`, `CANCELLED`, `WALLTIME_EXCEEDED`, `INTERRUPTED` |
| Node state | `PENDING_AGENT`, `AVAILABLE`, `DRAINING`, `DOWN`, `DECOMMISSIONING`, `DECOMMISSIONED` |
| Donation mode | `reserved`, `shared` |
| Drain mode | `soft`, `hard` |
| Alert kind | `UNIT_COUNT_MISMATCH`, `NODE_DOWN`, `UNIT_COUNT_CHANGED` |

### Walltime
`HH:MM`, for example `"04:00"` for four hours. Hours may have one to three digits (`"1:30"`, `"100:00"`).

---

## How a job runs

1. **Submit** ([POST /jobs](#post-jobs)). The answer comes at once: a request ID with `IN_PROCESS`, or a rejection. An accepted job is `QUEUED` immediately and a scheduling pass runs.
2. **Placed.** A pass ranks queued jobs and claims GPUs on one node for each job it can place: `SCHEDULED`. Jobs it cannot place stay `QUEUED` with a queue reason.
3. **Started.** The node's agent picks up the allocation and confirms the start: `RUNNING`.
4. **Ended.** The agent reports how the run ended; the GPUs are released and the job becomes `COMPLETED`, `FAILED`, `WALLTIME_EXCEEDED` or `CANCELLED`. Freed GPUs are given to waiting jobs at once.

If the platform stops a run (node failure, maintenance, hard drain) or the agent never confirms a start, the job goes back to `QUEUED` **with its original submit time**, and interrupted runs are not charged. A scheduling pass also runs every 10 seconds as a safety net.

---

## Jobs

### POST /jobs
Submits a GPU request.

**Auth:** any logged-in user with a GPU authorization (a lab and account type)

**Body**
| Field | Type | Rules |
|---|---|---|
| `gpus` | integer | positive whole number; all GPUs run on one node |
| `walltime` | string | `HH:MM`, at least `00:01`, at most `48:00` |
| `client_request_id` | string | 1–64 characters, unique per request; repeating it returns the original job |
| `project_ref` | string or null | optional, up to 255 characters |
| `snapshot_hash` | string or null | optional, up to 128 characters |

```json
{ "gpus": 2, "walltime": "04:00", "client_request_id": "cli-7f3a9c" }
```

**201** for a new request
```json
{ "request_id": 42, "status": "IN_PROCESS", "message": "Request submitted. Request ID: 42. Status: IN PROCESS" }
```

**200** for a retry with the same `client_request_id` within 24 hours: the original job, in its current state
```json
{ "request_id": 42, "status": "SCHEDULED", "message": "Request already submitted. Request ID: 42. Status: SCHEDULED" }
```

| Error | When |
|---|---|
| 400 with a [rejection code](#rejection-codes) | the request breaks a rule; nothing is stored |
| 403 `AUTHORIZATION_INACTIVE` | no GPU authorization |
| 409 `This client request ID was already used for an earlier request.` | the ID was used more than 24 hours ago |
| 422 | missing `client_request_id` |

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"gpus":2,"walltime":"04:00","client_request_id":"cli-7f3a9c"}'
```

---

### GET /jobs/{id}
What a job is doing right now: its state, why it is waiting and until when, where it runs, and how it ended.

**Auth:** the job's owner

**200** returns a [Job](#job)

| Error | When |
|---|---|
| 404 `Job not found.` | no such job, or it belongs to someone else |

---

### PATCH /jobs/{id}
Changes a `QUEUED` job. Only the fields you send change, and the same checks as [POST /jobs](#post-jobs) apply (including the course cap). A rejected edit leaves the job unchanged.

**Growing either GPUs or walltime moves the job to the back of the queue** (its submit time becomes now); keeping or reducing both keeps its place.

**Auth:** the job's owner

**Body**
| Field | Type | Rules |
|---|---|---|
| `gpus` | integer | optional, as in POST /jobs |
| `walltime` | string | optional, as in POST /jobs |
| `snapshot_hash` | string | optional, the snapshot after a configuration change |

```json
{ "gpus": 1, "walltime": "02:00" }
```

**200** returns the updated [Job](#job); a scheduling pass runs straight after.

| Error | When |
|---|---|
| 400 with a [rejection code](#rejection-codes) | the edit breaks a rule; the job is unchanged |
| 404 `Job not found.` | no such job, or it belongs to someone else |
| 409 `JOB_NOT_QUEUED` | the job was already placed, started or ended |

---

### POST /jobs/{id}/cancel
Cancels your job.
- `QUEUED`: cancelled at once.
- `SCHEDULED`: cancelled at once and its GPUs released.
- `RUNNING`: the node is asked to stop it; it becomes `CANCELLED`, and its GPUs are released, when the node confirms the process has stopped. Until then `stop_pending` is true and the job shows `stop_reason: "CANCELLED"`.

**Auth:** the job's owner

**Body:** none

**200**
```json
{ "id": 42, "state": "RUNNING", "stop_pending": true, "message": "Stopping: the job becomes CANCELLED once its node confirms the stop." }
```
```json
{ "id": 43, "state": "CANCELLED", "stop_pending": false, "message": "Job cancelled." }
```

| Error | When |
|---|---|
| 403 `You do not have permission to cancel this job.` | someone else's job (the attempt is logged) |
| 404 `Job not found.` | |
| 409 `Job 42 is already COMPLETED.` | the job has already ended |

---

### GET /jobs/quota
A course account's sponsorship cap and how much of it is used.

`remaining = cap − consumed − GPU-hours requested by the sponsorship's QUEUED, SCHEDULED and RUNNING jobs`

**Auth:** a course account with a cap

**200**
```json
{
  "cap_gpu_hours": 20.0,
  "consumed_gpu_hours": 4.5,
  "committed_gpu_hours": 2.0,
  "remaining_gpu_hours": 13.5,
  "members": [
    { "user_id": "alice23", "consumed_gpu_hours": 3.0 },
    { "user_id": "bob23", "consumed_gpu_hours": 1.5 }
  ]
}
```

| Error | When |
|---|---|
| 404 `No GPU-hour cap applies to your account.` | research accounts, or a course account without a cap |

---

## GPU nodes (admin)

All admin endpoints return **403 `Requires role: ADMIN`** to anyone else.

A node moves only along these states. Any other change is refused with **409** and leaves the node unchanged.

| From | To |
|---|---|
| `PENDING_AGENT` | `AVAILABLE` (first heartbeat with the registered GPU count) |
| `AVAILABLE` | `DRAINING`, `DOWN`, `DECOMMISSIONING` |
| `DRAINING` | `AVAILABLE` (return), `DOWN`, `DECOMMISSIONING` |
| `DOWN` | `AVAILABLE` (rejoin), `DECOMMISSIONING` |
| `DECOMMISSIONING` | `DRAINING` (abort), `DECOMMISSIONED` |

Only `AVAILABLE` nodes receive new jobs.

### POST /admin/nodes
Registers a GPU node with its units. It stays `PENDING_AGENT` until its agent's first heartbeat reports the registered GPU count. The response carries a **one-time enrolment token** for the node's agent, valid for 24 hours.

**Auth:** `ADMIN`

**Body**
| Field | Type | Rules |
|---|---|---|
| `name` | string | 1–100 characters, unique |
| `owner_lab_id` | integer | an existing lab |
| `donation_mode` | string | `reserved` (owner lab only) or `shared` |
| `teaching_pool` | boolean | default `false`; a teaching-pool node must be `shared` |
| `unit_count` | integer | 1–64 GPUs |

```json
{ "name": "nlp-01", "owner_lab_id": 1, "donation_mode": "shared", "teaching_pool": false, "unit_count": 4 }
```

**201**
```json
{ "node": { "id": 3, "name": "nlp-01", "state": "PENDING_AGENT", "unit_count": 4, "...": "..." }, "enrolment_token": "q0Xh...k2" }
```
`node` is a [Node](#node).

| Error | When |
|---|---|
| 400 `Unknown owner lab 9.` | |
| 400 `A teaching-pool node must be shared (FR-GPU-181).` | |
| 409 `A node named nlp-01 is already registered.` | |
| 422 | missing field, bad donation mode, unit count out of range |

---

### GET /admin/nodes
The pool at a glance: every node with its state, GPUs, free GPUs and open alerts.

**Auth:** `ADMIN`

**200** returns an array of [Node](#node)

---

### POST /admin/nodes/{id}/drain
Takes an `AVAILABLE` node out of service for repair. It receives no new jobs.
- `soft`: running jobs finish normally.
- `hard`: running jobs get 10 minutes (`drain_grace`), then are stopped and requeued with their original submit time, uncharged.

**Auth:** `ADMIN`

**Body**
| Field | Type | Rules |
|---|---|---|
| `mode` | string | `soft` (default) or `hard` |
| `reason` | string | required, 1–255 characters |

```json
{ "mode": "soft", "reason": "replace fan" }
```

**200** returns the [Node](#node), now `DRAINING`

| Error | When |
|---|---|
| 404 `Node 9 not found.` | |
| 409 `Node nlp-01 is PENDING_AGENT; it cannot become DRAINING.` | not allowed from the current state |
| 422 | missing or empty reason |

---

### POST /admin/nodes/{id}/return
Brings a `DRAINING` node back. It becomes `AVAILABLE` on its next heartbeat that reports the registered GPU count with no leftover containers. If it reports a different count, it stays `DRAINING` and a `UNIT_COUNT_CHANGED` alert is raised until you [confirm the new count](#post-adminnodesidunit-count).

**Auth:** `ADMIN`

**Body:** none

**200** returns the [Node](#node) with `"return_requested": true`

| Error | When |
|---|---|
| 409 `Node nlp-01 is AVAILABLE; only a DRAINING node can return to service.` | |

---

### POST /admin/nodes/{id}/decommission
Retires a node permanently, from `AVAILABLE`, `DRAINING` or `DOWN`. It receives no new jobs. `soft` lets running jobs end; `hard` stops them after `drain_grace`. Once it holds no running work it becomes `DECOMMISSIONED`: its agent's credential stops working, and its records (units, runs, usage) are kept. Queued jobs that no remaining node could ever run are then cancelled with reason `NO_CAPABLE_NODE`.

**Auth:** `ADMIN`

**Body:** as for [drain](#post-adminnodesiddrain)

```json
{ "mode": "soft", "reason": "end of warranty" }
```

**200** returns the [Node](#node), now `DECOMMISSIONING`

| Error | When |
|---|---|
| 409 `Node nlp-01 is PENDING_AGENT; it cannot become DECOMMISSIONING.` | |

---

### POST /admin/nodes/{id}/escalate
Turns a drain into a decommission (`DRAINING` → `DECOMMISSIONING`), keeping the drain mode.

**Auth:** `ADMIN`

**Body**
```json
{ "reason": "not worth repairing" }
```

**200** returns the [Node](#node)

| Error | When |
|---|---|
| 409 `Node nlp-01 is AVAILABLE; only a DRAINING node can be escalated.` | |

---

### POST /admin/nodes/{id}/abort-decommission
Cancels a decommission (`DECOMMISSIONING` → `DRAINING`).

**Auth:** `ADMIN`

**Body**
```json
{ "reason": "spare part arrived" }
```

**200** returns the [Node](#node)

| Error | When |
|---|---|
| 409 `Node nlp-01 is DRAINING; it cannot become DRAINING.` | not decommissioning |

---

### POST /admin/nodes/{id}/unit-count
Confirms a node's new GPU count after a repair. Extra GPUs are added; with fewer, the highest-numbered ones are retired (kept for history, never used again). Clears the unit-count alerts.

**Auth:** `ADMIN`

**Body**
```json
{ "unit_count": 3 }
```

**200** returns the [Node](#node)

| Error | When |
|---|---|
| 409 `Node nlp-01 is AVAILABLE; take it out of service first.` | the node must be `DRAINING`, `DOWN` or `PENDING_AGENT` |
| 409 `A unit to be removed still holds an allocation.` | |
| 422 | count out of 1–64 |

---

### GET /admin/alerts
Open node alerts: a new node reporting the wrong GPU count (`UNIT_COUNT_MISMATCH`), a node that stopped sending heartbeats (`NODE_DOWN`), a returning node with a changed count (`UNIT_COUNT_CHANGED`). An alert closes by itself when the node leaves that condition.

**Auth:** `ADMIN`

**200**
```json
[
  { "id": 5, "node_id": 3, "kind": "NODE_DOWN", "detail": "No heartbeat since 2026-10-15 09:12:41.", "raised_at": "2026-10-15T09:13:15" }
]
```

---

### POST /admin/alerts/{id}/acknowledge
Dismisses an open alert with a reason.

**Auth:** `ADMIN`

**Body**
```json
{ "reason": "known: two GPUs out for RMA" }
```

**204** no body

| Error | When |
|---|---|
| 404 `Alert 9 not found.` | |
| 409 `This alert is already closed.` | |

---

## Maintenance windows (admin)

A window covers selected GPU units (all of a node's units for whole-node maintenance). Jobs are never placed on a unit if they would still be running when its window starts. Runs still active when a window opens are stopped and requeued with their original submit time, uncharged.

### POST /admin/maintenance/preview
Shows, without changing anything, when the units will be clear and which running jobs a window starting at `starts_at` would stop.

**Auth:** `ADMIN`

**Body**
| Field | Type | Rules |
|---|---|---|
| `unit_ids` | integer array | at least one GPU unit |
| `starts_at` | date-time | |

```json
{ "unit_ids": [7, 8], "starts_at": "2026-10-16T10:00:00+05:30" }
```

**200**
```json
{
  "clear_by": "2026-10-16T09:00:30",
  "collisions": [
    { "job_id": 42, "owner": "alice23", "lab_id": 1, "gpu_hours_at_risk": 2.0, "guaranteed_end": "2026-10-16T09:00:30" }
  ]
}
```
- `clear_by`: every run on these units is guaranteed to have ended by then.
- `gpu_hours_at_risk`: GPU time the run will have used when the window stops it.

| Error | When |
|---|---|
| 400 `Choose at least one GPU unit.` | |
| 400 `Unknown GPU units: [99].` | |

---

### POST /admin/maintenance
Books a window. If it would stop running jobs, it is refused with **409** and the collision set unless `confirm` is `true`; you can instead move it after `clear_by`.

**Auth:** `ADMIN`

**Body**
| Field | Type | Rules |
|---|---|---|
| `unit_ids` | integer array | at least one GPU unit |
| `starts_at` | date-time | |
| `ends_at` | date-time | after `starts_at`, in the future |
| `reason` | string | required, 1–255 characters |
| `confirm` | boolean | default `false`; required when running jobs would be stopped |

```json
{ "unit_ids": [7, 8], "starts_at": "2026-10-16T10:00:00+05:30", "ends_at": "2026-10-16T12:00:00+05:30", "reason": "BIOS update" }
```

**201**
```json
{ "booking_id": "6d1c0e0a-...", "unit_ids": [7, 8], "starts_at": "2026-10-16T04:30:00", "ends_at": "2026-10-16T06:30:00", "reason": "BIOS update", "collisions": [] }
```

**409** when it would stop running jobs and `confirm` is false:
```json
{
  "detail": "The window starts before the units clear at 2026-10-16 09:00; it would stop 1 job(s). Confirm, move or narrow it.",
  "code": "MAINTENANCE_COLLISION",
  "clear_by": "2026-10-16T09:00:30",
  "collisions": [ { "job_id": 42, "owner": "alice23", "lab_id": 1, "gpu_hours_at_risk": 2.0, "guaranteed_end": "2026-10-16T09:00:30" } ]
}
```

| Error | When |
|---|---|
| 400 `A maintenance window must end after it starts.` | |
| 400 `A maintenance window must end in the future.` | |
| 400 `Unknown GPU units: [99].` | |
| 409 `MAINTENANCE_COLLISION` | see above |

---

### GET /admin/maintenance
Bookings that have not ended yet, earliest first.

**Auth:** `ADMIN`

**200** returns an array of [Booking](#booking)

---

### PATCH /admin/maintenance/{booking_id}
Moves or narrows a booking. Checked against running jobs again, as for booking.

**Auth:** `ADMIN`

**Body**
| Field | Type | Rules |
|---|---|---|
| `starts_at` | date-time | |
| `ends_at` | date-time | after `starts_at`, in the future |
| `confirm` | boolean | default `false` |

**200** returns the preview (`clear_by`, `collisions`) it was moved against

| Error | When |
|---|---|
| 404 `Maintenance booking ... not found.` | |
| 409 `MAINTENANCE_COLLISION` | still stops running jobs and not confirmed |

---

### DELETE /admin/maintenance/{booking_id}
Cancels a booking. Its units can take jobs again at once. Runs it already stopped are not restarted; their jobs are back in the queue.

**Auth:** `ADMIN`

**204** no body

| Error | When |
|---|---|
| 404 `Maintenance booking ... not found.` | |

---

## Node agent

These endpoints are called by the agent running on each GPU node, not by users. The agent **pulls** its work; the server never connects to a node. Every call after enrolment sends `X-Node-Credential`; an unknown, revoked or decommissioned node's credential gets **401 `Unknown or revoked node credential`**.

A reference agent with a simulated executor is in `backend/node_agent/simulated.py`.

### POST /agent/enrol
Exchanges the one-time enrolment token from [POST /admin/nodes](#post-adminnodes) for the node's credential. The credential is shown only here; store it.

**Auth:** none (the token is the proof)

**Body**
```json
{ "enrolment_token": "q0Xh...k2" }
```

**200**
```json
{ "node_id": 3, "credential": "Zr8W...uQ" }
```

| Error | When |
|---|---|
| 400 `This enrolment token is invalid, used or expired.` | |

---

### POST /agent/heartbeat
Sent every 10 seconds. After 30 seconds without one, the node becomes `DOWN` and its jobs are requeued with their original submit time, uncharged.

**Auth:** node credential

**Body**
| Field | Type | Rules |
|---|---|---|
| `unit_count` | integer | GPUs the agent sees |
| `running_allocation_ids` | integer array | allocations it is running a container for (default `[]`) |

```json
{ "unit_count": 4, "running_allocation_ids": [17] }
```

**200**
```json
{ "state": "AVAILABLE", "terminate_allocation_ids": [] }
```
- `terminate_allocation_ids`: containers to stop and clean up because their allocation is gone (after a node failure, for example). A node coming back from `DOWN` or a repair becomes `AVAILABLE` only once this list is empty and the GPU count matches.

---

### GET /agent/allocations
Everything this node should be running or stopping.

**Auth:** node credential

**200** returns an array of [Allocation](#allocation)

---

### POST /agent/allocations/{id}/acknowledge
Confirms the start. **Call it before starting anything.** The job becomes `RUNNING` and its walltime starts counting.

**Auth:** node credential

**Body:** none

**200**
```json
{ "started": true }
```
`"started": false` means this allocation was already acknowledged (a repeated delivery after an agent restart): **do not start it a second time**.

| Error | When |
|---|---|
| 404 `Allocation 17 not found.` | not this node's allocation |
| 409 `Allocation 17 is no longer live; do not start it.` | the job was cancelled or requeued meanwhile |

---

### POST /agent/allocations/{id}/report
Reports that the run ended **and** its workspace is cleaned up. Only then are the GPUs released.

**Auth:** node credential

**Body**
| Field | Type | Rules |
|---|---|---|
| `seq` | integer | ≥ 1, increasing with every report from this node; a report is applied at most once |
| `outcome` | string | `EXITED` (the process ended on its own), `WALLTIME` (the agent's walltime timer stopped it), `STOPPED` (stopped because `stop_reason` was set) |
| `exit_code` | integer or null | required for `EXITED` |

```json
{ "seq": 12, "outcome": "EXITED", "exit_code": 0 }
```

The job ends as follows:
| Report | Job state |
|---|---|
| any, while `stop_reason` is set | from the stop reason: `CANCELLED`, `WALLTIME_EXCEEDED`, or back to `QUEUED` for `MAINTENANCE` / `EMERGENCY_DRAIN` |
| `EXITED` with code 0 | `COMPLETED` |
| `EXITED` with another code | `FAILED` |
| `WALLTIME` | `WALLTIME_EXCEEDED` |

**200**
```json
{ "applied": true, "job_state": "COMPLETED" }
```
`{"applied": false, "job_state": null}` means this `seq` was already applied; nothing changed.

| Error | When |
|---|---|
| 400 `An EXITED report needs the exit code.` | |
| 404 `Allocation 17 not found.` | not this node's allocation |
| 409 `Allocation 17 was never started.` | |

---

## Metrics

### GET /metrics
Scheduler metrics in the Prometheus text format. Counts only, never job contents.

**Auth:** none

**200** (`text/plain`)
```
# HELP prayog_queue_depth Jobs waiting in QUEUED.
# TYPE prayog_queue_depth gauge
prayog_queue_depth 3
# HELP prayog_free_units Unallocated GPU units on AVAILABLE nodes, outside maintenance.
# TYPE prayog_free_units gauge
prayog_free_units 5
# HELP prayog_pass_duration_seconds Scheduling pass duration.
# TYPE prayog_pass_duration_seconds summary
prayog_pass_duration_seconds_sum 1.732
prayog_pass_duration_seconds_count 120
# HELP prayog_last_pass_duration_seconds Duration of the most recent scheduling pass.
# TYPE prayog_last_pass_duration_seconds gauge
prayog_last_pass_duration_seconds 0.012
# HELP prayog_claim_failures_total Claim attempts that did not place the job.
# TYPE prayog_claim_failures_total counter
prayog_claim_failures_total 2
# HELP prayog_dispatch_timeouts_total Allocations released because the agent never acknowledged.
# TYPE prayog_dispatch_timeouts_total counter
prayog_dispatch_timeouts_total 0
```

---

## Objects

### Job
| Field | Type | Description |
|---|---|---|
| `id` | integer | the request ID |
| `state` | string | [job state](#enum-values) |
| `queue_reason` | string or null | `QUEUED` only: why it is waiting |
| `p0_free_at` | date-time or null | `QUEUED` on your own lab's node only because its GPUs are busy: when enough are guaranteed free |
| `gpus` | integer | |
| `walltime` | string | `HH:MM` |
| `submitted_at` | date-time | when it was submitted (never changes) |
| `node_id`, `node_name` | integer / string or null | `SCHEDULED` or `RUNNING`: where |
| `unit_indexes` | integer array | `SCHEDULED` or `RUNNING`: which GPUs on that node |
| `started_at` | date-time or null | confirmed start of the current or last run |
| `walltime_ends_at` | date-time or null | `RUNNING`: when walltime stops it |
| `stop_reason` | string or null | `RUNNING`: a stop in progress (`CANCELLED`, `WALLTIME_EXCEEDED`, `MAINTENANCE`, `EMERGENCY_DRAIN`) |
| `planned_stop_at`, `planned_stop_reason` | date-time / string or null | `RUNNING`: a maintenance window will stop it, and why |
| `ended_at` | date-time or null | how the last run ended: when |
| `end_state` | string or null | [run end state](#enum-values) |
| `exit_code` | integer or null | |
| `cancel_reason` | string or null | who cancelled it (`owner`), or why it was cancelled |
| `dispatch_attempts` | integer | times a node failed to confirm the start; the job fails after 3 |

```json
{
  "id": 42,
  "state": "RUNNING",
  "queue_reason": null,
  "p0_free_at": null,
  "gpus": 2,
  "walltime": "04:00",
  "submitted_at": "2026-10-15T08:00:00",
  "node_id": 3,
  "node_name": "nlp-01",
  "unit_indexes": [0, 1],
  "started_at": "2026-10-15T08:00:04",
  "walltime_ends_at": "2026-10-15T12:00:04",
  "stop_reason": null,
  "planned_stop_at": null,
  "planned_stop_reason": null,
  "ended_at": null,
  "end_state": null,
  "exit_code": null,
  "cancel_reason": null,
  "dispatch_attempts": 0
}
```

### Node
| Field | Type | Description |
|---|---|---|
| `id` | integer | |
| `name` | string | |
| `owner_lab_id` | integer | |
| `donation_mode` | string | `reserved` / `shared` |
| `teaching_pool` | boolean | |
| `state` | string | [node state](#enum-values) |
| `state_reason` | string or null | the admin's reason for a drain or decommission |
| `drain_mode` | string or null | `soft` / `hard`, while draining or decommissioning |
| `return_requested` | boolean | a return to service is waiting for a healthy heartbeat |
| `unit_count` | integer | registered GPUs |
| `free_units` | integer | GPUs not allocated |
| `reported_unit_count` | integer or null | GPUs the agent last reported |
| `last_heartbeat_at` | date-time or null | |
| `open_alerts` | integer | |

### Allocation
What a node agent receives from [GET /agent/allocations](#get-agentallocations).

| Field | Type | Description |
|---|---|---|
| `allocation_id` | integer | the idempotency key: never start the same allocation twice |
| `job_id` | integer | |
| `unit_indexes` | integer array | start only on these GPUs |
| `walltime_minutes` | integer | |
| `snapshot_hash` | string or null | the workspace to prepare |
| `started_at` | date-time or null | null until acknowledged |
| `walltime_ends_at` | date-time or null | enforce locally: stop the run then and report `WALLTIME` |
| `stop_reason` | string or null | when set: stop the run and report `STOPPED` |

### Booking
| Field | Type |
|---|---|
| `booking_id` | string |
| `unit_ids` | integer array |
| `starts_at`, `ends_at` | date-time, UTC |
| `reason` | string |
| `collisions` | array, filled only in the response to booking |

---

## Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/jobs` | GPU-authorized user | submit a GPU request |
| GET | `/jobs/{id}` | owner | job state, queue reason, placement, run |
| PATCH | `/jobs/{id}` | owner | edit a queued job |
| POST | `/jobs/{id}/cancel` | owner | cancel a job |
| GET | `/jobs/quota` | course account | sponsorship cap and use |
| POST | `/admin/nodes` | ADMIN | register a node, get its enrolment token |
| GET | `/admin/nodes` | ADMIN | all nodes |
| POST | `/admin/nodes/{id}/drain` | ADMIN | take out of service (soft / hard) |
| POST | `/admin/nodes/{id}/return` | ADMIN | bring a drained node back |
| POST | `/admin/nodes/{id}/decommission` | ADMIN | retire a node (soft / hard) |
| POST | `/admin/nodes/{id}/escalate` | ADMIN | drain → decommission |
| POST | `/admin/nodes/{id}/abort-decommission` | ADMIN | decommission → drain |
| POST | `/admin/nodes/{id}/unit-count` | ADMIN | confirm a changed GPU count |
| GET | `/admin/alerts` | ADMIN | open node alerts |
| POST | `/admin/alerts/{id}/acknowledge` | ADMIN | dismiss an alert |
| POST | `/admin/maintenance/preview` | ADMIN | clear-by time and jobs a window would stop |
| POST | `/admin/maintenance` | ADMIN | book a window |
| GET | `/admin/maintenance` | ADMIN | upcoming windows |
| PATCH | `/admin/maintenance/{booking_id}` | ADMIN | move or narrow a window |
| DELETE | `/admin/maintenance/{booking_id}` | ADMIN | cancel a window |
| POST | `/agent/enrol` | enrolment token | get the node credential |
| POST | `/agent/heartbeat` | node | liveness and GPU inventory |
| GET | `/agent/allocations` | node | work to run or stop |
| POST | `/agent/allocations/{id}/acknowledge` | node | confirm a start |
| POST | `/agent/allocations/{id}/report` | node | a run ended; release its GPUs |
| GET | `/metrics` | none | scheduler metrics (Prometheus) |

---

## Running it locally

From the `backend/` folder, with the database running (`docker compose up -d`):

```bash
..\.venv\Scripts\alembic.exe upgrade head
..\.venv\Scripts\python.exe -m scripts.seed_gpu_demo "DemoPassword123"     # labs and demo users
..\.venv\Scripts\python.exe -m uvicorn main:app --reload                    # the API
..\.venv\Scripts\python.exe -m logic.scheduler.run                         # second terminal: the tick (passes and timeouts)
```

The seed creates `gpuadmin@iitk.ac.in` (ADMIN), `nlp.researcher@iitk.ac.in` and `robotics.researcher@iitk.ac.in` (research accounts) and `cs455.student@iitk.ac.in` (a course account with a 20 GPU-hour cap), all with the password you give, and the labs NLP, Robotics, Central and "CS455 course group".

Register a node as the admin with [POST /admin/nodes](#post-adminnodes), then start its agent with the returned token (third terminal):

```bash
..\.venv\Scripts\python.exe -m node_agent.simulated --units 4 --state-dir C:\prayog-agents\node1 --enrolment-token <token> --run-seconds 60
```

Log in as a researcher, [submit a job](#post-jobs), and follow it with [GET /jobs/{id}](#get-jobsid): `SCHEDULED` → `RUNNING` → `COMPLETED` after 60 seconds.

Scheduler settings (walltime limits, timeouts, weights) can be changed with `PRAYOG_SCHED_<NAME>` environment variables, for example `PRAYOG_SCHED_MAX_WALLTIME_MINUTES=360`. An invalid setting stops the API at startup with the reason.
