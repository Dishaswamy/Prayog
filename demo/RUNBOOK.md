# Demo runbook

Ten minutes on the live deployment (https://prayog-smoke.onrender.com), driven from the `prayog` CLI and the web
portal, on the six simulated GPU nodes in this folder. Commands are for PowerShell on Windows, run from the
repository root unless a step says otherwise.

| Segment | Time | Slot |
|---|---|---|
| [1. Normal end-to-end workflow](#1-normal-end-to-end-workflow) | 1:30 | 0:00–1:30 |
| [2. Agentic AI: the Steward](#2-agentic-ai-the-steward) | 2:00 | 1:30–3:30 |
| [3. Concurrency and no double allocation](#3-concurrency-and-no-double-allocation) | 2:30 | 3:30–6:00 |
| [4. Failure handling and recovery](#4-failure-handling-and-recovery) | 1:30 | 6:00–7:30 |
| [5. Security and unauthorised access](#5-security-and-unauthorised-access) | 1:00 | 7:30–8:30 |
| [6. Jira → GitHub → test traceability](#6-jira--github--test-traceability) | 1:30 | 8:30–10:00 |

## Where to run it from

The kit is in `demo/`. Until both pull requests are merged, use a checkout of branch **`demo-day`**, which
combines the fixes (`fix-demo-findings`) and this kit (`demo-kit`). After they are merged, use `main`.

**One-time setup on the demo laptop:**

```powershell
git fetch origin; git switch demo-day
py -3.12 -m venv "$env:USERPROFILE\.venvs\prayog"
& "$env:USERPROFILE\.venvs\prayog\Scripts\python.exe" -m pip install -e .\cli
```

**The fake nodes belong to the machine that registered them.** Their credentials are in `demo/state/`, which is
never committed. Either run the demo on that machine, or copy its `demo/state/` folder to the demo laptop (and
never run both). The last option is to register a fresh set as the admin, after renaming the nodes in
`nodes.json`, since names are unique: `python demo/fake_gpus.py setup --server … --email ADMIN@iitk.ac.in`.

## Before the day

1. **Run the scheduler tick on the server.** It marks a silent node DOWN and requeues its job; segment 4 needs it.
   - **If the server is deployed from a branch that includes `fix-demo-findings`:** on a Render web service, set the
     start command to `sh scripts/start_single_container.sh`. It runs the tick (restarted if it exits), the
     Steward worker when configured, and the API with one worker.
   - **Otherwise:** use the same processes inline:
     `sh -c "python -m logic.gpu.scheduler.run & uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"`.
   - **Check:** kill a fake node, and it shows DOWN within about 40 s.
2. **Set up the Steward.** Segment 2 needs it; without it every `/steward/*` route is 404.
   1. With `POSTGRES_*` pointing at the server's database, run `python -m scripts.setup_steward` in `backend/`.
   2. On the server, set `STEWARD_DB_USER`, `STEWARD_DB_PASSWORD`, `STEWARD_INPROCESS_REASONING=heuristic` (or
      `claude` with `ANTHROPIC_API_KEY`) and `STEWARD_PUBLIC_BASE_URL`.
   3. Check `/steward/healthz`, then rehearse segment 2 once.
3. **Deploy for the last time, then prepare.** Deploying `fix-demo-findings` also removes the upload freeze, which
   froze the server twice on 12 Nov 2026 (the fake nodes already avoid it). A redeploy on Render wipes uploaded
   files (versions, checkpoints, logs). After it, upload a fresh version of the project you will demo (`prayog project update`), and pre-run:
   - one reference run and its rebuild, so the compare in segment 1 is instant;
   - a walltime overrun and a crash, so their results are ready in segment 4.
4. **Logins.** Have four ready: a student or researcher with approved research access (CLI), the platform admin
   (browser window A and the concurrency script), the professor who approved that access (browser window B, a
   private window), and optionally a second researcher. Keep passwords off the screen. Never demo a wrong-password
   lockout on these accounts: 10 failures lock the account.

## 15 minutes before

1. **Terminal 1, the fake nodes.** Leave it running, and start only one copy.
   ```powershell
   & "$env:USERPROFILE\.venvs\prayog\Scripts\python.exe" demo\fake_gpus.py start
   ```
2. **Terminal 2, the student CLI.** A login lasts 60 minutes.
   ```powershell
   Set-Alias prayog "$env:USERPROFILE\.venvs\prayog\Scripts\prayog.exe"
   prayog login -e STUDENT@iitk.ac.in -r student --api-url https://prayog-smoke.onrender.com
   prayog whoami
   ```
   Then `cd` into the linked project folder.
3. **Terminal 3, the scripts.** At the repository root, with the same alias.
4. **Browser A (admin).** Open `https://prayog-smoke.onrender.com/app/` on the GPU pool page, and check that all
   six `fake-*` nodes are AVAILABLE.
5. **Browser B (professor).** The same portal, in a private window, on the professor view.
6. **Tabs for segment 6.** The Jira story PRAYOG-53, GitHub PRs #5 and #21, the GitHub Actions run, and
   `Deliverables/D3/11_Traceability_Matrix.md`.

Below, `V` is the letter of the version uploaded after the final deploy, and `PROJECT` is the project's name.

## 1. Normal end-to-end workflow

*0:00, 1:30. Student CLI, then the admin and professor windows.*

1. Submit a run. It queues through the same scheduler a real GPU would use.
   ```powershell
   prayog run PROJECT --walltime 00:05 --hp fake_seconds=40 --hp job=live
   prayog project status PROJECT          # QUEUED, then RUNNING, metrics arriving
   ```
2. **Admin window:** a fake node's units are now held by that job.
3. **CLI, about 40 s later:** the run is SUCCEEDED and sealed as a snapshot. Download it:
   ```powershell
   prayog project download PROJECT V/2 --to .\out
   ```
4. **Reproducibility,** using the rebuild prepared beforehand:
   ```powershell
   prayog project compare PROJECT V/<rebuild>   # every metric ok, weights identical, REPRODUCED
   ```
5. **Professor window:** the student's project and runs, as read-only metadata. No code is shown and nothing can
   be changed.

> "Code is uploaded as content-addressed pieces; datasets are only fingerprinted. A rebuild must match the
> original data exactly by BLAKE3, and compare checks every metric and the weights byte for byte."

## 2. Agentic AI: the Steward

*1:30, 2:00. Student CLI and web, then admin.*

1. A run that crashes, whose log ends in a CUDA illegal-memory-access error:
   ```powershell
   prayog run PROJECT --walltime 00:05 --hp fake_seconds=20 --hp fake_fail=true --hp job=crash
   prayog project status PROJECT          # FAILED, exit code 1, the end of its log
   ```
2. **Student web view:** the Steward's diagnosis of that run (S1). It gives the likely cause, cites the evidence
   and makes a proposal; the student approves or rejects it. The Steward proposes, a person decides.
3. **Admin window:** Steward hardware warnings (S2). Investigate, then freeze, drain or dismiss.
4. **Optional:** a failed rebuild gives a single-use link to a chat about it (S3); a successful one gives
   optimisation advice (S4).

> "Logs are sanitised before anything reaches the model. Its output is validated by the server, the Runtime holds
> no database credentials, and diagnostic data sits behind row-level security."

**If the Steward is not running:** say so plainly. Show the crash's log in `status`, the evaluation set
(`steward/eval/s1_cases.jsonl`, run by `steward/tools/evaluate.py`) and `steward/DESIGN.md`.

## 3. Concurrency and no double allocation

*3:30, 2:30. Terminal 3, then the CLI and the admin window.*

1. **The race script.** It asks for the admin password, then releases every request at the same instant.
   ```powershell
   & "$env:USERPROFILE\.venvs\prayog\Scripts\python.exe" demo\concurrency_demo.py --admin-email ADMIN@iitk.ac.in --cleanup
   ```
   On 12 Nov 2026: 8 submissions with one ID created 1 job; of 10 simultaneous 4-GPU requests, 4 ran (8 + 4 + 4
   GPUs filled) and 6 queued; no unit was held by two jobs, and no job was split across nodes.
2. **Admin window:** refresh the pool and point at each unit and the single job holding it.
3. **Two refusals:**
   ```powershell
   prayog run PROJECT --gpus 9 --walltime 00:10      # the largest node you can use has 8
   prayog run PROJECT --walltime 00:10 --hp job=twice --hp fake_seconds=120
   prayog run PROJECT --walltime 00:10 --hp job=twice --hp fake_seconds=120   # DUPLICATE_ACTIVE_RUN
   ```
4. **While the queue is full,** point at the idle T4 nodes. They are in the teaching pool, closed to research
   accounts. `status` names why a job waits, for example `INSUFFICIENT_FREE_UNITS`.

> "Correctness rests on PostgreSQL row locks and partial unique indexes, not on application locks. In the D3
> control without them, units were double-booked in 99 of 100 rounds; with them, never, across six race tests run
> four times."

## 4. Failure handling and recovery

*6:00, 1:30. Terminals 2 and 3, and the admin window.*

1. **Node failure.** Start a long job, see which node took it in terminal 1, and kill that node. About 40 s later
   the node is DOWN, the job is QUEUED again (`NODE_FAILURE`), and it starts on another node. Then revive the
   killed node.
   ```powershell
   prayog run PROJECT --gpus 2 --walltime 00:10 --hp fake_seconds=150 --hp job=survivor
   & "$env:USERPROFILE\.venvs\prayog\Scripts\python.exe" demo\fake_gpus.py kill fake-rtx-2x
   prayog project status PROJECT
   & "$env:USERPROFILE\.venvs\prayog\Scripts\python.exe" demo\fake_gpus.py revive fake-rtx-2x
   ```
2. **Runs that fail on their own** (prepared beforehand): a walltime overrun shows
   `FAILED (WALLTIME_EXCEEDED)` and keeps its checkpoints; a crash shows its exit code and the end of its log.
3. **Cancelling** stops a running job through its node, with two-phase release: its GPUs are freed only after the
   node confirms.

> "In the D3 drills we killed the agent, the API, the database and the tick under load: a node was detected DOWN in
> 8.9 s and its job running again in 14.8 s, and no data was lost."

**If the tick is not running:** skip the kill. Show the prepared walltime and crash runs, and cite drill F1 in
`Deliverables/D3/06_Failure_Recovery_Report.md`.

## 5. Security and unauthorised access

*7:30, 1:00. Terminal 3.*

1. Read-only probes, made with the student's login:
   ```powershell
   & "$env:USERPROFILE\.venvs\prayog\Scripts\python.exe" demo\security_demo.py
   ```
   On 12 Nov 2026, 11 of 11 were refused, each with its reason. They cover no login, a forged token, a student
   reaching admin or professor routes, approving an access request, someone else's job, and a fake node
   credential.
2. **CLI:** before login, `prayog --help` lists only `login`. After it, a student's `prayog help` has no admin or
   review commands. The server checks every request again anyway.

> "Passwords are Argon2-hashed, tokens expire in 60 minutes and can be revoked, and 10 failures lock an account.
> Node credentials are stored only as hashes. On real nodes, user code runs in a container with no network, a
> read-only root and no added capabilities."

## 6. Jira → GitHub → test traceability

*8:30, 1:30. Browser tabs.* Follow the requirement that segment 3 showed live:

| Link | Show |
|---|---|
| Requirement | `NFR-GPU-001` / `FR-GPU-055`: one live holder per GPU unit |
| Jira | story `PRAYOG-53` |
| GitHub | PRs #5 and #21 (the branch names carry the Jira key) |
| Code | the claim path in `backend/logic/gpu/scheduler/claim.py`, which cites the requirement ID |
| Test | `test_passes_racing_for_the_last_free_unit_allocate_it_once` in `test_claim` (27 tests) and `test_concurrency` (6) |
| Result | green in GitHub Actions, and live in segment 3 |

Close on `Deliverables/D3/11_Traceability_Matrix.md`. It is generated by `src/make_trace.py` from the Jira export,
the merged PRs and the test results. It covers 103 backlog items: 84% done in Jira, 82% mapped to automated tests.
Jira keys are the plan keys plus 4, in case someone asks why the numbers differ from the sprint plan.

## On the day

| Do | Avoid |
|---|---|
| Say up front that the GPU nodes are simulated: the scheduler, agents, upload and verification paths are the real code; only training is replaced by a timer. | Redeploying after the final preparation: it wipes uploaded files. |
| Give each job a distinct `--hp job=…`, unless you are showing the duplicate refusal. | Running `fake_gpus.py start` twice. |
| Log in with `--api-url` within the hour before. | Wrong-password demos on real accounts (lockout after 10). |
| Allow a minute for the first request: a free Render instance wakes slowly. | |
