# Prayog CLI

`prayog` is the command-line client for the Prayog server. It handles signing in, access requests, and the whole
ML workflow: uploading a project folder as versions, running it on the GPU cluster, reading and downloading the
results, and rebuilding a run to check it can be reproduced.

All command-line tooling lives in this package. The server's HTTP API is documented in `backend/ML_FLOW.md`.

```bash
pip install prayog          # installs the CLI only: no login, no access
prayog login                # sign in
prayog help                 # what you can do right now
```

From this repo: `pip install -e ./cli`. Python 3.9 or newer.

- [Quick tour](#quick-tour)
- [What is available when](#what-is-available-when)
- [Command reference](#command-reference): [account](#account-commands) · [access](#access-commands) · [reviewing requests](#reviewing-requests-professors-and-admins) · [projects and versions](#project-commands) · [runs](#running-on-the-gpus) · [results and rebuilds](#results-and-rebuilds)
- [How it works](#how-it-works)
- [Configuration](#configuration) · [Errors and exit codes](#errors-and-exit-codes) · [Tests](#tests) · [Not yet possible](#not-yet-possible)

## Quick tour

```bash
prayog login -e you@iitk.ac.in -r student
prayog project create vision                       # a project on the server
prayog project link vision ./my-folder             # upload the folder: version A
prayog run vision --gpus 2 --walltime 04:00 --data ./data --hp lr=0.01
prayog project status vision                       # watch it: queued, running, results
prayog project download vision A/1 --to ./out      # the best checkpoint, log and metrics
prayog run vision --rebuild A/1 --walltime 04:00 --data ./data
prayog project compare vision A/2                  # did it reproduce?
```

## What is available when

| State | Commands |
|---|---|
| Not logged in | `login`, `help`, and `-h`. Every other name is answered with "You are not logged in". |
| Logged in, any role | `whoami`, `logout`, `help`, `--version` |
| Student, researcher, professor | `project …`, `run` |
| Student, researcher | `access status`, `access request` |
| Professor, admin | `requests list`, `requests show`, `requests approve`, `requests reject` |
| Admin | `requests revoke` |

Before login, `prayog --help` lists only `login`. Hiding the other commands is a courtesy: the server checks every
request again, so a command you can see may still be refused. After login, `prayog help` shows exactly what your role can
use, and `prayog help <command>` describes one command.

## Command reference

Every command prints what happened and, when something fails, what to do next. Arguments in `UPPER CASE` are values you
supply; `[brackets]` are optional.

### Account commands

#### `prayog login [-e EMAIL] [-r ROLE] [--api-url URL] [--password-stdin] [--allow-insecure-http]`
Signs in and saves a login token on this computer.

| Option | Meaning |
|---|---|
| `-e`, `--email` | your institute email, such as `name@iitk.ac.in` (asked for if omitted) |
| `-r`, `--role` | `student`, `researcher`, `professor` or `admin`; it must match your account (asked for if omitted) |
| `--api-url` | the Prayog server; default `$PRAYOG_API_URL`, else `http://localhost:8000` |
| `--password-stdin` | read the password from standard input, for scripts, instead of prompting |
| `--allow-insecure-http` | allow sending the password over plain `http` to a server that is not on this machine |

The password is asked for without echo and is never stored; only the access token is saved (see
[Configuration](#configuration)). Plain `http` is refused except to this machine. Login is refused for a user with no
current access, and for students and researchers it then prints where their access stands. Logging in again replaces the
saved login and ends the old session on the server.

```bash
prayog login                                               # prompts for email, role, password
prayog login -e you@iitk.ac.in -r student                  # prompts for the password only
echo "$PW" | prayog login -e you@iitk.ac.in -r student --password-stdin
```

#### `prayog logout`
Ends your session on the server and removes the saved token from this computer. The local token is removed even if the
server cannot be reached (you get a warning that it may stay valid there until it expires). `prayog` itself stays
installed.

#### `prayog whoami`
Shows who you are logged in as: email, user ID, role, and which server.

#### `prayog help [COMMAND]`
Shows the same text as `--help`, limited to what your role can use. Without a command it lists them; with one it describes
that command.

### Access commands

Requests for access are made in the web portal, not here. These commands show where yours stands. (Students and
researchers.)

#### `prayog access status`
Lists your access requests: ID, purpose, the professor's and the admin's decision, and the state in plain words
(`ACTIVE until <date>`, `PENDING`, `REJECTED`, `EXPIRED`, `REVOKED`, or `ENDING` while unfinished jobs complete).

#### `prayog access request`
Prints how to request access (log in to the web portal and submit the form there, then check with `access status`). It does
not submit anything.

### Reviewing requests (professors and admins)

Professors see the requests addressed to them; admins see all.

#### `prayog requests list [--status PENDING|APPROVED|REJECTED|REVOKED]`
A table of requests: ID, name, role, purpose, the professor and admin decisions, the final state, and the start and end
dates.

#### `prayog requests show ID`
One request in full, including roll number, course and branch, and its current state.

#### `prayog requests approve ID` · `prayog requests reject ID`
Records your decision on a request and prints the state it is now in.

#### `prayog requests revoke ID` (admin)
Ends an approved user's access. If the user has unfinished jobs the revocation waits until they complete, and no new work
is accepted meanwhile.

### Project commands

A project is the thing you train: a name on the server, plus the versions of your folder uploaded to it and the runs made
from them. Names are unique among your projects, ignoring upper and lower case, and are matched the same way when you type
them. (Students, researchers, professors.)

#### `prayog project create NAME [-d DESCRIPTION]`
Creates an empty project. The name may be up to 120 characters and the description up to 4000. Prints the project's
unique ID.

#### `prayog project link NAME FOLDER`
Uploads a folder to the project for the first time. It becomes version **A** and the current version.

The server keeps its own copy, so a run or rebuild never depends on files that may change on your disk. Code, configs,
lockfiles and seeds are uploaded in pieces, and a piece the server already holds is not sent again. Dataset files (`.csv`,
`.tsv`, `.parquet`, `.xlsx`, `.jsonl`, `.npy`, `.npz`, `.sqlite`, …) are **fingerprinted, not uploaded**; use `run --data`
to supply data. Never read or uploaded: `.env` files, keys and certificates (`.pem`, `.key`, …), credential and secret
files, `.git`, virtual environments, `node_modules`, caches, `build`/`dist`, and anything else listed under
[How it works](#what-is-uploaded). A symbolic link anywhere in the folder makes the upload fail, so nothing outside the
folder can slip in.

It records `FOLDER/.prayog/link.json` (the project, and the versions uploaded from this folder), which `update` uses.

#### `prayog project update [FOLDER]`
Uploads what changed in a linked folder (default: the current folder) as the next version: B, C, and so on. It becomes the
current version. Only the parts that changed are sent.

- Nothing changed since the last upload: it says so and uploads nothing.
- The folder now matches a version stored earlier: no new version is created; that version is made current again.

#### `prayog project versions NAME`
Lists the versions, newest first:

```
VERSION                 FILES  RUNS  UPLOADED    ID
-------  -------  ----  -----  ----  ----------  ------------
B                 head  4      1     2026-10-22  b9b15d461457
A        current        4      3     2026-10-22  a2059dc46ce0
```

**head** is the newest version; **current** is the one `prayog run` uses unless you name another. They are the same until
you use `checkout`. `ID` is the start of the BLAKE3 digest that identifies the version's contents.

#### `prayog project current NAME`
Says which version is current and whether it is the newest.

#### `prayog project checkout NAME VERSION`
Makes `VERSION` (such as `A`) the current version, so new runs use it. It changes no version and deletes nothing; the next
`update` still adds a new version on top. To run another version just once, use `prayog run --version` instead.

#### `prayog project pull NAME DESTINATION [--version V] [--data-dir DIR]`
Restores a stored version into a **new** folder (it must not exist), checking every piece against its recorded checksum;
nothing is left behind if any check fails. Without `--version` it restores the current version. Datasets were never stored,
so if the version recorded dataset fingerprints, `--data-dir` must point to a folder holding your copies, which are checked
against them.

#### `prayog project clean NAME`
Deletes the project's temporary dataset copies now instead of waiting for them to expire. A copy a running job still needs
is kept. Run results and the datasets' fingerprints are never deleted.

### Running on the GPUs

#### `prayog run NAME --walltime HH:MM [--gpus N] [--version V] [--data PATH]… [--entrypoint FILE] [--hp KEY=VALUE]… [--rebuild V/N]`
Queues a run on the GPU cluster and returns at once; it does not wait for it to finish. The run is numbered within its
version: `A run 1`, `A run 2`, `B run 1`, …

| Option | Meaning |
|---|---|
| `--walltime HH:MM` | **required.** The longest the run may take, such as `04:00`. The run is stopped at this limit. |
| `--gpus N` | GPUs on one node (default 1) |
| `--version V` | run this version once (default: the current version) |
| `--data PATH` | a dataset file or folder to upload for this run; repeatable. Paths inside the run are relative to what you gave (`--data ./data` puts `data/train.csv` at `train.csv`). |
| `--entrypoint FILE` | the Python file to run (default `main.py`) |
| `--hp KEY=VALUE` | a hyperparameter; repeatable. The value is read as JSON when it can be: `--hp lr=0.01 --hp epochs=20 --hp name=resnet --hp layers=[64,128]` |
| `--rebuild V/N` | reproduce a finished run instead; see [rebuilds](#prayog-run-name---rebuild-vn) |

What happens: the dataset is fingerprinted with BLAKE3 and only the files the server lacks are uploaded; the job is queued
through the normal scheduler, so access checks, quotas and queue limits apply; the run records the dataset's fingerprint.
Retrying after a dropped connection never submits twice. Uploaded data is deleted **two days after the run ends**, or
sooner with `prayog project clean`, and never while a job still needs it.

**Your program.** It is started as `python main.py` (or `--entrypoint`) in your uploaded code directory, in a container with
no network, and finds everything through the environment:

| Variable | Meaning |
|---|---|
| `PRAYOG_HYPERPARAMETERS_JSON` | your `--hp` values as a JSON object |
| `PRAYOG_DATA_DIR` | the dataset, read-only |
| `PRAYOG_OUTPUT_DIR` | where to write results |

It writes into `PRAYOG_OUTPUT_DIR`:

- `checkpoints/step-<N>.<ext>`: a checkpoint at step `N`. Use `step-<N>-best.<ext>` for the best so far. Write to a name
  ending `.tmp` and rename it when finished, so a half-written file is never sent.
- `metrics.jsonl`: one JSON object per line, `{"name": "loss", "value": 0.42, "step": 10}`.

Standard output and error become the run's log. These are sent to the server **while the run trains**, with no command
needed, so a failed or cancelled run keeps what it saved. A successful run is then sealed as a snapshot.

```python
import json, os, pathlib
hp = json.loads(os.environ["PRAYOG_HYPERPARAMETERS_JSON"])
data = pathlib.Path(os.environ["PRAYOG_DATA_DIR"]) / "train.csv"
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"]); (out / "checkpoints").mkdir()
for step in range(1, 4):
    with open(out / "metrics.jsonl", "a") as m:
        m.write(json.dumps({"name": "loss", "value": 1 / step, "step": step}) + "\n")
    tmp = out / "checkpoints" / f"step-{step}.tmp"; tmp.write_bytes(b"...weights...")
    tmp.rename(out / "checkpoints" / f"step-{step}.bin")
```

The container image already holds your program's dependencies; nothing is installed at run time.

### Results and rebuilds

#### `prayog project runs NAME [--version V]`
Lists the project's runs, newest first, with status, GPUs, walltime, number of checkpoints and the date submitted. `--version`
limits it to one version's runs.

#### `prayog project status NAME [--run V/N]`
Shows the project's current version and the state of a run (the latest, or the one named, such as `--run A/2`):

```
Project 'vision': current version A, 1 version(s), 3 run(s).
A run 1: SUCCEEDED (COMPLETED)
  2 GPU(s), walltime 04:00
  started 2026-10-22 10:05:00, ended 2026-10-22 11:05:00
  metrics (last value): acc=0.91, loss=0.25
  checkpoints: 5 saved (steps 1 to 5), best at step 5
  dataset: 1 files (2.0 KB), 1 still staged
  sealed as snapshot 8d1c…
```

Run states: `QUEUED` (with the reason it is waiting), `RUNNING`, `SUCCEEDED`, `FAILED` (with the exit code and the end of
its output, or "stopped at the walltime limit"), `CANCELLED`. If the platform stops a run (maintenance, node failure) it
goes back to `QUEUED` and starts over; the interrupted attempt's partial results are discarded. A rebuild is labelled
`a rebuild of A run 1`. It also reports the temporary dataset copies still held and when they go.

#### `prayog project download NAME V/N --to DIR [--checkpoint WHICH] [--no-log] [--no-metrics]`
Downloads what a run saved into `DIR` (created if needed):

```
DIR/checkpoints/step-2-best      the checkpoint
DIR/run.log                      the program's output
DIR/metrics.json                 every metric value: [{"name", "step", "value"}, …]
```

`--checkpoint` chooses which: `best` (default; the last one if none is marked best), `last`, `all`, `none`, or a step
number. Every file is checked against the size and SHA-256 the server recorded and is saved only if it matches. An existing
file is never overwritten.

#### `prayog run NAME --rebuild V/N`
Reproduces a finished run, such as `A/1`. The rebuild uses the original's version, entrypoint and hyperparameters (so
`--version`, `--entrypoint` and `--hp` are refused with it), and **the data you give must match the original's recorded
fingerprint exactly**, checked by BLAKE3. If it does not, nothing is queued and the files that differ are named. You may
choose different `--gpus` and `--walltime`. The data had been deleted by then, so give it again with `--data`.

```bash
prayog run vision --rebuild A/1 --walltime 04:00 --data ./data
```

A run that does not exist, is not finished, or has no verified snapshot gets the same "cannot be rebuilt" answer, so the
message reveals nothing about runs that are not yours.

#### `prayog project compare NAME V/N [--tolerance T]`
Checks a rebuild (`V/N`, such as `A/2`) against the run it reproduces: each metric the original logged against the
rebuild's, and whether the saved weights are the very same bytes, compared by BLAKE3.

```
A run 2 (SUCCEEDED) against A run 1, tolerance 0:
METRIC  STEP  ORIGINAL  REBUILT  DIFFERENCE
------  ----  --------  -------  ----------
loss    1     1         1        0           ok
loss    2     0.5       0.5      0           ok
Weights: identical, byte for byte (BLAKE3 abababababab)
Result: REPRODUCED
```

`--tolerance` is the largest allowed absolute difference per metric (default 0, exactly equal). A metric the rebuild never
logged counts as different. `REPRODUCED` means the rebuild succeeded and every original metric matched; identical weights
are the stronger, bit-exact result. **Matching can only be expected if your program is deterministic** (fixed random seeds,
deterministic operations); otherwise the result honestly says it did not reproduce.

## How it works

### What is uploaded
`link` and `update` send a deterministic archive of your folder's code, cut into pieces. Each piece is stored under its
BLAKE3 digest, so identical content is kept once on the server no matter how many versions, runs or people use it, and
anything already stored is not sent again. A manifest lists every file (path, size, SHA-256, role) and the pieces in order;
the BLAKE3 digest of the manifest identifies the version. Excluded from upload: `.env` and `.env.*` (except
`.env.example`), `credentials*`, `secrets*`, `*.tfstate`, SSH, cloud and kubeconfig files, private keys and certificates,
`.git`/`.hg`/`.svn`, `.venv`/`venv`/`env`, `node_modules`, `__pycache__` and tool caches, `.idea`, `.terraform`,
`artifacts`, `results`, `build`, `dist`, and the `.prayog` folder itself.

### Datasets are not kept
Training data is large, so the server does not store it. It is uploaded for a run, a GPU node reads it, and the server
deletes it two days after the last run using it ends. The run keeps each file's path, size and BLAKE3 fingerprint for good,
so a rebuild can insist on exactly the same data, and `status` shows that the copy was deleted but the fingerprint kept. A
second run on the same data uploads nothing while the copy is still held.

### Versions and runs
Each distinct upload is a version (A, B, C, …); uploading content identical to an earlier version re-selects it. Within a
version, runs are numbered 1, 2, 3 …, so "A run 2" always names the same thing. Every run records its code version,
hyperparameters, dataset fingerprint, metrics, checkpoints and log, which is what makes a rebuild possible.

## Configuration

| | |
|---|---|
| `PRAYOG_API_URL` | the server to log in to when `--api-url` is not given (default `http://localhost:8000`). The URL is saved with the token and later commands only ever send the token there. |
| `PRAYOG_CONFIG_DIR` | where the login is kept (default `~/.prayog`; directory mode `0700`, file `0600`) |
| `PRAYOG_PORTAL_URL` | the web portal's address, shown by `prayog access request` |

The login file holds the access token, never the password. Tokens expire after 60 minutes; `prayog logout` ends the session
on the server.

## Errors and exit codes

Messages say what happened and what to do. Exit code `0` is success, `1` failure, `2` bad usage (a mistyped option, a
folder that is not valid, a check that failed locally).

| Situation | Message starts with |
|---|---|
| no saved login | You are not logged in. Run `prayog login`. |
| wrong email, password or role | Invalid email, password or role |
| saved token rejected | The server did not accept your saved login |
| token older than 60 minutes, or password was reset | Your login session has expired. Run `prayog login`. |
| access period over | Your approved access period has ended |
| access revoked | Your access has been revoked |
| refused for your role | the server's reason |
| server unreachable | Could not reach the Prayog server (nothing was changed) |
| unknown project | You have no project named 'x'. Create it first with `prayog project create x`. |
| no version uploaded yet | 'x' has no uploaded version yet. Upload a folder with `prayog project link x FOLDER`. |
| dataset deleted or not uploaded | These dataset files must be uploaded first |
| rebuild data differs | A rebuild must use exactly the original run's data … |

## Tests

```bash
pip install -e './cli[test]'
pytest cli
```

The CLI tests use a mock HTTP transport; no server is needed. The backend suite also drives this CLI against the real
server (`backend/tests/test_cli_end_to_end.py`), which needs `pip install -e ./cli` first.

## Not yet possible

- **Access extension**: no backend endpoint yet, so no CLI command.
- **Cancelling a run** from the CLI: a queued or running job can be cancelled through the jobs API (`POST /jobs/{id}/cancel`),
  but there is no `prayog` command for it yet.
- **Infrastructure management and audit review** have API endpoints but no CLI commands yet.
- **Environment capture**: a run records its entrypoint, GPUs and walltime, not the container image or installed packages,
  so a rebuild on a different image is not detected.
