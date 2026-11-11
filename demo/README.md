# Demo kit

Everything used to demo Prayog on a live server without real GPUs: six simulated GPU nodes, two scripts
for the concurrency and security segments, two small sample projects, and the demo runbook.

| Path | What it is |
|---|---|
| [`fake_gpus.py`](fake_gpus.py) | Simulated GPU nodes. Each one runs the real node agent; only training is replaced. |
| [`nodes.json`](nodes.json) | The six nodes' layout: GPUs, sharing, teaching pool, owner lab |
| [`concurrency_demo.py`](concurrency_demo.py) | Live race tests against the server: same-ID submissions, a burst for the GPUs, then a check that no unit has two holders |
| [`security_demo.py`](security_demo.py) | Read-only probes that must all be refused: no login, forged token, wrong role, someone else's job, fake node credential |
| [`sample-projects/`](sample-projects/) | `cv` (a toy segmentation) and `linear-regression` (with a small dataset), both following the run contract |
| [`RUNBOOK.md`](RUNBOOK.md) | The ten-minute demo: preparation, setup, and each segment with commands and fallbacks |
| `state/` | Saved node identities, created by `setup`. Git-ignored: it holds node credentials, so the nodes can only be started on the machine that registered them. |

Run everything from the repository root, with Python 3.11 or newer and `httpx` and `blake3` installed
(the CLI's environment has both: `pip install -e ./cli`).

## Simulated GPU nodes

**Nothing is trained and no GPU is used.** Each node runs Prayog's own agent (`backend/node_agent/agent.py`),
so enrolment, heartbeats, allocations, acknowledgement, code and data download and verification, result
upload, walltime and stop handling are the project's own code paths. Only the training process is replaced:
`FakeRunner` "trains" on a timer and writes generated metrics (`loss`, `accuracy`), checkpoints and a log. To the
server they are ordinary nodes.

| Node | GPUs | Sharing | Who can use it |
|---|---|---|---|
| `fake-a100-8x` | 8 | shared | research accounts |
| `fake-a100-4x` | 4 | shared | research accounts |
| `fake-v100-4x` | 4 | reserved | the owner lab only |
| `fake-rtx-2x` | 2 | shared | research accounts |
| `fake-t4-2x` | 2 | shared, teaching pool | course (COURSE_PROJECT) accounts only |
| `fake-t4-1x` | 1 | shared, teaching pool | course (COURSE_PROJECT) accounts only |

All are owned by lab 1 in `nodes.json`; change `owner_lab_id` for another server (`GET /professors` shows each
professor's lab). A research account in that lab can use 18 GPUs; the three T4s are closed to it.

### Register once, as an admin

```bash
python demo/fake_gpus.py setup --server https://prayog-smoke.onrender.com --email ADMIN@iitk.ac.in
```

This registers every node in `nodes.json` (`POST /admin/nodes`) and keeps each one-time enrolment token in
`state/`. A node already set up here is skipped.

### Run them

```bash
python demo/fake_gpus.py start
```

Leave it running: the nodes exist only while it does. Ctrl+C stops them. The next `start` reconnects with the
saved credentials, without registering again. **Run one `start` at a time:** two would share `state/` and fight
over the nodes.

Simulate a node failure from a second terminal:

```bash
python demo/fake_gpus.py kill fake-a100-8x      # stops heartbeating; DOWN after the heartbeat timeout
python demo/fake_gpus.py revive fake-a100-8x    # heartbeats again
```

A killed node is marked DOWN only when the server's scheduler tick (`python -m logic.gpu.scheduler.run`) is
running. On a one-process host, start the server with `backend/scripts/start_single_container.sh` (added on
branch `fix-demo-findings`).

### Steering a fake run

Pass these as hyperparameters to `prayog run`:

| `--hp` | Effect |
|---|---|
| `fake_seconds=N` | how long it "trains" (default 60, or `start --run-seconds`) |
| `fake_steps=N` | how many steps it logs and checkpoints (default 10) |
| `fake_fail=KIND` | crashes with the log a real failure of that kind leaves, which is what the Steward diagnoses: `bug` (or `true`: a CUDA illegal memory access), `oom` (CUDA out of memory), `hardware` (an NVIDIA Xid "GPU has fallen off the bus"), `import` (a missing module) or `data` (a missing dataset file). `bug`, `oom` and `hardware` fail 60% of the way through, `import` and `data` at start; all exit with code 1 (`fake_fail=N` for exit code N) |

The Steward's diagnosis (S1) of each kind differs, even with the offline `heuristic` reasoning: out of memory,
library version, missing data, hardware or code bug. An admin hardware warning (S2) needs failures on the same
GPU unit from **two different users** within 24 hours, so use `fake_fail=hardware` from two accounts.

Results are deterministic: the same code version and hyperparameters always give the same metrics and
byte-identical checkpoints, so a rebuild is reported as `REPRODUCED`. Plain jobs (`POST /jobs`, as the
concurrency script submits) are "run" for `--run-seconds` and report success.

Two runs with the same version, entrypoint, hyperparameters and data cannot be active at once
(`DUPLICATE_ACTIVE_RUN`), so give parallel runs a distinguishing `--hp job=…`.

## The demo scripts

Both use the student login saved by `prayog login` (`~/.prayog/credentials.json`).

```bash
python demo/concurrency_demo.py --admin-email ADMIN@iitk.ac.in --cleanup   # asks for the admin password
python demo/security_demo.py                                               # changes nothing
```

Results against the Render deployment on 12 Nov 2026: 8 simultaneous submissions with one ID created one job;
10 simultaneous 4-GPU requests gave 4 running (8 + 4 + 4 GPUs) and 6 queued, with no unit held by two jobs and
no job split across nodes; 11 of 11 unauthorised requests were refused.

## The sample projects

Both follow the run contract in [`cli/README.md`](../cli/README.md): hyperparameters from
`PRAYOG_HYPERPARAMETERS_JSON`, data from `PRAYOG_DATA_DIR`, metrics and checkpoints into `PRAYOG_OUTPUT_DIR`.
They also run on a plain machine (`python main.py`), writing to `output/`.

```bash
prayog project create cv
prayog project link cv demo/sample-projects/cv
prayog run cv --walltime 00:05 --hp fake_seconds=30

prayog project create linear-regression
prayog project link linear-regression demo/sample-projects/linear-regression
prayog run linear-regression --walltime 00:05 --data demo/sample-projects/linear-regression/data
```

`link` writes `.prayog/link.json` into the folder; it is git-ignored.

## Known server-side issues found while preparing the demo

- **Upload freeze.** `store_artifact` used to hold the experiment's row lock across an `await`, so two
  checkpoint uploads to one experiment at once could freeze an API worker for good (even `/health` hung). Fixed
  on branch `fix-demo-findings`. The fake nodes also send their uploads one at a time, which keeps an
  unfixed server safe.
- **A redeploy on Render wipes uploaded files.** The artifact store is the container's disk. Database rows
  survive, so old runs still list, but their versions fail with "Artifact data is missing". Upload a fresh
  version after a redeploy (change a file, then `prayog project update`), or configure a persistent disk or the S3
  storage backend.
- **The Steward must be set up separately**: `python -m scripts.setup_steward` against the server's database,
  its environment variables, and its worker. Without it, every `/steward/*` route is 404.
