# Prayog

Prayog is a GPU-computing platform for IIT Kanpur students and researchers. It allows users to request access to shared GPU resources, manage machine-learning projects, submit experiments, monitor their progress, and reproduce previous runs.

Prayog provides two main ways to interact with the platform:

- **Web Portal:** Register, request GPU access, check access status, and view projects and experiment information.
- **Command-Line Interface (CLI):** Upload project code, manage versions, submit GPU runs, inspect results, download artifacts, and rebuild experiments.

This guide explains how to use Prayog, from requesting GPU access to running experiments and retrieving results. It also documents the available CLI commands and explains how project data and experiment artifacts are stored.

## What can I do with Prayog?

- **Request GPU access:** Submit an access request for approval by your supervising Professor and an administrator.
- **Use shared GPU resources:** Submit jobs with a requested GPU count and walltime.
- **Manage ML projects:** Upload code as versioned snapshots and organise experiment runs.
- **Run experiments:** Submit code with datasets and hyperparameters.
- **Monitor results:** Track run status and retrieve logs, metrics, and checkpoints.
- **Reproduce experiments:** Rebuild previous runs using their recorded code version, settings, and dataset fingerprints.
- **Get AI-assisted support:** Use optional Steward features when they are enabled and configured.

## Choose how you want to use Prayog

| If you are... | Start here |
|---|---|
| A student or researcher | [Getting started](#1-getting-started) |
| Running experiments from your terminal | [Installing and using the CLI](#2-installing-and-using-the-cli) |
| Learning the experiment workflow | [Running an experiment](#3-running-an-experiment) |
| A Professor | [Role-specific workflows](#6-role-specific-workflows) |
| An administrator | [Role-specific workflows](#6-role-specific-workflows) |
| Looking for technical details | [Further documentation](#9-further-documentation) |
| Building, configuring or running the server, or running the tests | [Running Prayog yourself](#running-prayog-yourself) |

---

## Running Prayog yourself

This section is for developers, reviewers and administrators who want to build and run the platform. The server is a FastAPI application, a scheduler process (the *tick*) and PostgreSQL 16. GPU nodes connect to it through a node agent.

### Prerequisites

- Git
- **Option A:** Docker with Compose v2 (Docker Desktop on Windows and macOS)
- **Option B:** Python 3.12, plus PostgreSQL 16 (or Docker to run PostgreSQL only)

### Configure

Copy the template and fill in real values. `.env` is git-ignored; never commit it.

```bash
cp .env.example .env              # Windows PowerShell: Copy-Item .env.example .env
```

| Variable | What to set |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Database account and name. With option A, the database is created with these values. |
| `POSTGRES_HOST`, `POSTGRES_PORT` | `localhost` and `5432` for option B. Option A sets the host itself. |
| `JWT_SECRET_KEY` | A long random string, for example from `python -c "import secrets; print(secrets.token_urlsafe(48))"`. The API refuses to start without it. |
| `STEWARD_ENABLED` | `false` to start with. See [Optional: the Steward](#optional-the-steward). |

### Option A: the whole platform with Docker Compose (recommended)

```bash
docker compose -f docker-compose.deploy.yml up -d --build
```

This starts four services:

- `postgres`: PostgreSQL 16. It is not published to the host.
- `migrate`: runs `alembic upgrade head` once, then exits.
- `api`: the API and web app, with 4 workers on `127.0.0.1:8000`.
- `tick`: the scheduler, which runs a scheduling pass every 10 seconds.

Check it is up at `http://127.0.0.1:8000/health`, then open `http://127.0.0.1:8000/app`.

Create the first administrator (admins cannot self-register):

```bash
docker compose -f docker-compose.deploy.yml exec api python -m scripts.create_admin admin@iitk.ac.in "<strong password>"
```

For a local trial, `docker compose -f docker-compose.deploy.yml exec api python -m scripts.seed_gpu_demo` adds sample labs and test accounts. Every test account's password is its user ID reversed, so never run it on a shared deployment.

Stop with `docker compose -f docker-compose.deploy.yml down`. Add `-v` to also delete the database and stored artifacts.

To serve the platform to other machines, put TLS in front of port 8000. [Deliverables/D3/10_Deployment_Guide.md](Deliverables/D3/10_Deployment_Guide.md) covers this, along with systemd services, nginx, backups and upgrades.

### Option B: run from source (development)

```bash
python -m venv .venv
source .venv/bin/activate         # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt

docker compose up -d postgres     # or use your own PostgreSQL 16

cd backend
alembic upgrade head              # create the schema
alembic check                     # should report no new upgrade operations
python -m scripts.create_admin admin@iitk.ac.in "<strong password>"
uvicorn main:app --reload --port 8000
```

In a second terminal, from `backend/` with the same virtual environment, start the scheduler:

```bash
python -m logic.gpu.scheduler.run
```

Labs have no API yet. `python -m scripts.seed_gpu_demo` creates sample labs. For a real deployment, insert them once as described in the Deployment Guide, section 4.

### Add a GPU node

1. Sign in as the administrator and register the node in the web app's pool view, or with `POST /admin/nodes` (see [API_GPU.md](API_GPU.md)). The response shows a one-time enrolment token, valid for 24 hours.
2. On the GPU machine, build the run image once, then start the agent with that token:

   ```bash
   docker build -t prayog-ml-runtime:python3.12 -f backend/worker/Dockerfile backend/worker
   cd backend
   python -m node_agent.agent --server http://<api-host>:8000 --units 4 \
       --state-dir /var/lib/prayog-agent --runner docker --image prayog-ml-runtime:python3.12 \
       --enrolment-token <token>
   ```

   The agent exchanges the token for its own credential on first contact, then heartbeats and pulls work. The node turns `AVAILABLE` in the admin view.

To try the scheduler without a GPU, use the simulated agent instead:

```bash
python -m node_agent.simulated --server http://127.0.0.1:8000 --units 4 --state-dir .agent-state --enrolment-token <token>
```

### Optional: the Steward

The Steward is off unless `STEWARD_ENABLED=true`. To turn it on:

1. Set `STEWARD_DB_USER`, `STEWARD_DB_PASSWORD` and `STEWARD_PUBLIC_BASE_URL` in `.env`.
2. From `backend/`, run `python -m scripts.setup_steward` once. It creates the Steward's own database role, which cannot bypass row-level security.
3. Run the worker beside the API: `python -m integration.steward_worker`.

The default reasoning adapter is rule-based and needs no API key (`STEWARD_INPROCESS_REASONING=heuristic`). See [steward/README.md](steward/README.md) for the other adapters.

### Run the tests and coverage

The backend tests need PostgreSQL, which you can start with `docker compose up -d postgres`, and the `.env` values above. Run them from the repository root:

```bash
pip install -e ./steward -e ./cli
PYTHONPATH=backend PRAYOG_RUN_SCHEDULER_POSTGRES_TESTS=1 PRAYOG_RUN_PROFESSOR_POSTGRES_TESTS=1 \
    pytest -q test_cases --cov=backend --cov-config=backend/.coveragerc --cov-report=term --cov-report=html
```

On Windows PowerShell, set the variables first:

```powershell
$env:PYTHONPATH="backend"; $env:PRAYOG_RUN_SCHEDULER_POSTGRES_TESTS="1"; $env:PRAYOG_RUN_PROFESSOR_POSTGRES_TESTS="1"
pytest -q test_cases --cov=backend --cov-config=backend/.coveragerc --cov-report=term --cov-report=html
```

- The coverage report is written to `htmlcov/index.html`. CI fails the build below 80%.
- The GPU scheduler tests create and use their own database, `prayog_scheduler_test`.
- A few tests use Linux-only features (process groups, symlinks, file permissions), so run on Linux or rely on CI for a clean result.
- Tests that start real Docker containers run only with `PRAYOG_RUN_DOCKER_SMOKE=1`. CI runs them on every pull request.

CLI tests: `cd cli && pip install -e ".[test]" && pytest -q`.

The same steps run in CI on every pull request; see [.github/workflows/mlflow-validation.yml](.github/workflows/mlflow-validation.yml).

---

## 1. Getting started

### 1.1 Open the web portal

Open the Prayog web application using the server address provided by your administrator.

For a local development instance, the default addresses are:

- **Web application:** `http://127.0.0.1:8000/`
- **Interactive API documentation:** `http://127.0.0.1:8000/docs`
- **Health check:** `http://127.0.0.1:8000/health`

The root URL redirects to the web application.

### 1.2 Register and request GPU access

Students and researchers can get started by following these steps:

1. Register using your IIT Kanpur email address, or sign in if you already have an account.
2. Navigate to the GPU access section.
3. Submit an access request with the required academic details, purpose, and supervising Professor information.
4. Wait for your supervising Professor to review the request.
5. An administrator must also approve the request before GPU access becomes active.
6. Once approved, sign in to the portal to view your account and available platform features.

**Important:** Submitting an access request does not automatically grant GPU access. Both the required Professor and administrator approvals must be completed before you can submit GPU jobs.

### 1.3 Check your access status

You can view your GPU access request and approval status through the web portal.

After installing the CLI and signing in (see [Section 2](#2-installing-and-using-the-cli)), you can also check your access status using:

```bash
prayog access status
```
---

## 2. Installing and using the CLI

The `prayog` CLI is the command-line client for managing projects and the machine-learning experiment lifecycle.

### 2.1 Prerequisites

- Python 3.9 or newer.
- Access to a running Prayog server.
- An account with the required platform permissions.

Installing the CLI does not create an account or grant GPU access.

### 2.2 Installation

From a clone of this repository, create a virtual environment and install the CLI:

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Install the CLI
python -m pip install -e ./cli
```

### 2.3 Sign in

Run:

```bash
prayog login
```

Follow the prompts for your institute email, role, and password.

You can also provide your email and role directly:

```bash
prayog login -e you@iitk.ac.in -r student
```
The role must match your account. Supported roles are `student`, `researcher`, `professor`, and `admin`.

By default, the CLI uses the URL configured through the `PRAYOG_API_URL` environment variable. If it is not set, the default is `http://localhost:8000`.

You can override the server URL when logging in:

```bash
prayog login --api-url https://YOUR-PRAYOG-SERVER
```

The CLI stores an access token locally, not your password. The saved login is associated with the server used during login.
**Access requirement:** CLI login requires current platform access. If your access has not yet been approved or is no longer active, check your request through the web portal. For students and researchers, the CLI reports the access status when login is refused.

Useful account commands:

```bash
prayog whoami
prayog logout
prayog help
```

- `whoami` displays the currently signed-in account.
- `logout` ends the session and removes the locally saved token.
- `help` displays commands available to your account.

### 2.4 Check available commands

The commands available to you depend on your login state and role.

Before logging in, run `prayog --help` to view the available login-related help. After logging in, use `prayog help` to see the commands available to your account.

```bash
prayog help
```

To view help for a particular command or command group:

```bash
prayog help project
```

The server independently checks permissions when commands are executed.

---

## 3. Running an experiment

This section walks through a typical student or researcher workflow, from uploading project code to reproducing a previous run.

Run the commands from your project workspace. Replace example names and paths with your own.

### Step 1: Create a project

Create a project on Prayog:

```bash
prayog project create vision
```

A project groups uploaded code versions and the experiment runs created from those versions.

### Step 2: Upload your project folder

Upload your code folder as the first project version:

```bash
prayog project link vision ./my-training-code
```

The first uploaded version is labelled `A`.

Prayog creates a versioned snapshot of the project folder. The server maintains its own copy, so later changes to your local files do not modify an already-uploaded version.

### Step 3: Submit an experiment

Submit a run with the required GPU count, walltime, dataset, and hyperparameters:

```bash
prayog run vision --gpus 2 --walltime 04:00 --data ./data --hp lr=0.01
```

The command queues the run and returns without waiting for it to finish.

| Option | Purpose |
|---|---|
| `--gpus N` | Number of GPUs requested on one node. Defaults to 1. |
| `--walltime HH:MM` | Maximum execution time. Required. |
| `--version V` | Version to run. Defaults to the current version. |
| `--data PATH` | Dataset file or folder to supply to the run. Can be repeated. |
| `--entrypoint FILE` | Python entrypoint. Defaults to `main.py`. |
| `--hp KEY=VALUE` | Hyperparameter to pass to the program. Can be repeated. |

The scheduler applies the platform's access checks, quotas, and queue limits.

Your Python program runs using the configured container environment. By default, Prayog executes `main.py`; you can specify another file using `--entrypoint`.

During execution, the following environment variables are available to your program:

| Variable | Purpose |
|---|---|
| `PRAYOG_DATA_DIR` | Location of the supplied dataset (read-only). |
| `PRAYOG_HYPERPARAMETERS_JSON` | Hyperparameters passed through `--hp`, available as a JSON object. |
| `PRAYOG_OUTPUT_DIR` | Directory where your program should save checkpoints and metrics. |

The program's standard output and error are captured as run logs. See the [CLI README](cli/README.md) for the expected checkpoint and metrics formats.

### Step 4: Monitor the run

Check the current project and run status:

```bash
prayog project status vision
```

List previous runs:

```bash
prayog project runs vision
```

Runs are numbered within their versions. For example:

- `A/1`: First run using version A.
- `A/2`: Second run using version A.
- `B/1`: First run using version B.

A run can be `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, or `CANCELLED`.

### Step 5: Download results

After a run completes, download its results:

```bash
prayog project download vision A/1 --to ./results
```

The downloaded results can include:

- Checkpoints.
- Run logs.
- Recorded metrics.

By default, the CLI downloads the best available checkpoint, along with logs and metrics.

### Step 6: Update your project

When you modify your code, upload the changes as a new version:

```bash
prayog project update
```

If the folder contents have changed, Prayog creates the next version (B, C, and so on) and makes it the current version.

If nothing has changed, no new version is uploaded. If the folder matches a version uploaded earlier, that version is selected again.

Only changed content needs to be uploaded.

### Step 7: Rebuild a previous run

To reproduce a completed run, provide its original run identifier and the same dataset:

```bash
prayog run vision --rebuild A/1 --walltime 04:00 --data ./data
```

The rebuild uses the original code version, entrypoint, and hyperparameters. The supplied dataset must match the original dataset fingerprints exactly.

### Step 8: Compare the rebuild

Compare the rebuild with its original run. Use the rebuild's run identifier; the following example assumes it is `A/2`:

```bash
prayog project compare vision A/2
```

The comparison checks recorded metrics and whether saved weights match byte-for-byte.

**Note:** Reproducibility depends on the experiment being deterministic. Randomness or nondeterministic operations can result in different metrics or weights.

---


## 4. Complete CLI command reference

This section lists the implemented CLI commands and their purposes, grouped by functionality. Commands and options available to a user depend on their role and login state. The server independently verifies permissions when a command is executed.

For detailed examples, validation rules, and explanations of all options, see the [CLI README](cli/README.md).

### 4.1 Account and authentication commands

| Command | Purpose |
|---|---|
| `prayog --version` | Display the installed CLI version. |
| `prayog login [-e EMAIL] [-r ROLE] [--api-url URL] [--password-stdin] [--allow-insecure-http]` | Sign in to a Prayog account and save the login token locally. |
| `prayog logout` | End the current session and remove the locally saved token. |
| `prayog whoami` | Display the currently signed-in account and server. |
| `prayog help [COMMAND]` | List commands available to the current account or display help for a specific command. |

The login command supports the roles `student`, `researcher`, `professor`, and `admin`. Options allow users to specify their email, role, server URL, or provide a password through standard input.

The global `--help` and `-h` options also display command help. Available commands are restricted according to the user's login state and role.

### 4.2 GPU access commands

These commands are available to students and researchers.

| Command | Purpose |
|---|---|
| `prayog access status` | Display your access requests, approval decisions, and current access state. |
| `prayog access request` | Display instructions for submitting an access request through the web portal. |

**Note:** `prayog access request` does not submit an access request. Requests must be submitted through the web portal.

### 4.3 Reviewing access requests

These commands are available to Professors and administrators, subject to their permissions.

| Command | Purpose |
|---|---|
| `prayog requests list [--status PENDING\|APPROVED\|REJECTED\|REVOKED]` | List access requests, optionally filtered by status. |
| `prayog requests show ID` | Display the details of a particular access request. |
| `prayog requests approve ID` | Approve an access request. |
| `prayog requests reject ID` | Reject an access request. |
| `prayog requests revoke ID` | Revoke an approved user's access. Administrator-only. |

Professors can view requests addressed to them, while administrators can view all requests.

### 4.4 Project and version management

Project commands are available to students, researchers, and Professors, subject to server permissions.

| Command | Purpose |
|---|---|
| `prayog project create NAME [-d DESCRIPTION]` | Create a new project, optionally providing a description. |
| `prayog project link NAME FOLDER` | Upload a project folder for the first time as a versioned snapshot. |
| `prayog project update [FOLDER]` | Upload changes from a previously linked folder as a new version. |
| `prayog project versions NAME` | List the uploaded versions of a project. |
| `prayog project current NAME` | Display the project's current version. |
| `prayog project checkout NAME VERSION` | Select a version to use for future runs. |
| `prayog project pull NAME DESTINATION [--version V] [--data-dir DIR]` | Restore a stored project version into a new local folder, optionally validating its dataset files. |
| `prayog project clean NAME` | Delete temporary staged dataset copies immediately. Copies still needed by running jobs are retained; run results and dataset fingerprints are not deleted. |

### 4.5 Run submission

| Command | Purpose |
|---|---|
| `prayog run NAME --walltime HH:MM [--gpus N] [--version V] [--data PATH]... [--entrypoint FILE] [--hp KEY=VALUE]... [--rebuild V/N]` | Submit an experiment to the GPU scheduler or rebuild a previously completed run. |

The `--walltime` option is required. Other options allow users to specify the GPU count, project version, dataset paths, Python entrypoint, and hyperparameters.

The `--rebuild` option reproduces an earlier run using its recorded code version and settings.

### 4.6 Monitoring, results, and rebuilds

| Command | Purpose |
|---|---|
| `prayog project runs NAME [--version V]` | List runs associated with a project, optionally filtered by version. |
| `prayog project status NAME [--run V/N]` | Display project status or inspect a particular run. |
| `prayog project download NAME V/N --to DIR [--checkpoint WHICH] [--no-log] [--no-metrics]` | Download a run's checkpoints, logs, and metrics, with options to select what to retrieve. |
| `prayog run NAME --rebuild V/N` | Reproduce a previously completed run using the original run's recorded configuration and matching dataset. |
| `prayog project compare NAME V/N [--tolerance T]` | Compare a rebuild with its original run, including metrics and saved weights. |

For rebuilds, the original dataset must be supplied again and must match the recorded dataset fingerprints.

For the complete command syntax, all supported options, examples, and CLI behaviour, see the **[CLI README](cli/README.md)**.


---

## 5. Data and storage

Prayog separates platform records, versioned project code, experiment outputs, and training datasets.

### 5.1 Where is data stored?

| Component | What it stores | Purpose |
|---|---|---|
| **PostgreSQL database** | User accounts, access requests, project and version records, run states, metrics, dataset fingerprints, and experiment metadata. | Maintains structured platform records and experiment information. |
| **Artifact storage** | Uploaded code snapshots, code chunks, checkpoints, logs, and other run artifacts. | Stores versioned code and experiment outputs. |
| **Dataset staging** | Temporary copies of datasets supplied for runs. | Makes training data available to GPU nodes during execution. |
| **Local CLI configuration** | Login token and project-link metadata. | Maintains the user's CLI session and links local folders to projects. |

The artifact storage backend is configured by the platform administrator. Prayog supports private local storage and S3-compatible object storage.
PostgreSQL stores the platform's structured records, while the actual code files, checkpoints, logs, and other artifacts are stored separately. Dataset files are staged temporarily for experiment execution rather than maintained as permanent project snapshots.

### 5.2 Project code and versions

When you run:

```bash
prayog project link vision ./my-training-code
```

Prayog inventories the folder and uploads the eligible project content as a versioned snapshot.

- Each distinct project snapshot is assigned a version (A, B, C, and so on).
- Versions are identified using the BLAKE3 digest of their project manifest.
- Identical content chunks are reused instead of being uploaded again.
- If an uploaded folder matches a previously stored version, Prayog selects that existing version rather than creating a duplicate.
- When project contents change, the updated snapshot is recorded as a new version. The server maintains its own copy independently of your local folder.

Common secrets and generated files are excluded from uploads, including `.env` files, private keys, virtual environments, caches, version-control metadata, and build directories. Symbolic links are refused.

Always review your project folder before uploading it. Do not put credentials, API keys, or private data in project files.

### 5.3 Dataset handling

Datasets are handled separately from project code.

- Dataset files are fingerprinted during project linking but are **not uploaded as part of the project snapshot**.
- Supply the actual dataset when submitting a run using `--data`.
- Prayog calculates BLAKE3 fingerprints for dataset files and records their paths, sizes, and fingerprints.
- Dataset copies are staged for GPU execution.
- By default, staged dataset files are deleted 48 hours after the last run using them ends. The platform administrator can configure this retention period.
- A running job's dataset is not deleted while it is still in use.
- Dataset fingerprints remain recorded after the temporary copies are deleted.

For a rebuild, you must supply the original dataset again. Prayog verifies that its fingerprints match the original run.

### 5.4 Run results and artifacts

During execution, the experiment can produce:

- **Checkpoints:** Saved model states.
- **Metrics:** Training measurements recorded at different steps.
- **Logs:** Standard output and error from the experiment.

These results are stored as experiment artifacts and can be retrieved using:

```bash
prayog project download vision A/1 --to ./results
```

The CLI verifies each downloaded artifact against its recorded file size and SHA-256 checksum before saving it. Existing files are not overwritten.

### 5.5 Local CLI files

The CLI maintains local configuration and project-link information:

- The login token is stored in the Prayog configuration directory (by default, `~/.prayog`).
- A linked project folder contains `.prayog/link.json`, which records its project association and uploaded versions.

The login file stores the access token, not the user's password.

---

## 6. Role-specific workflows

Available features depend on the signed-in role and the permissions configured on the server.

### 6.1 Students and researchers

Students and researchers can:

1. Register and sign in through the web portal.
2. Submit a GPU access request.
3. Wait for Professor and administrator approval.
4. Check their access status.
5. Create projects and upload code through the CLI.
6. Submit and monitor GPU experiments.
7. Download results and rebuild previous runs.

### 6.2 Professors

Professors can:

- Review access requests assigned to them.
- Approve or reject requests addressed to them.
- View read-only experiment records for currently assigned students and researchers with active approved relationships.
- Review, approve, or reject project handover requests addressed to them.

Professor experiment views are read-only and scoped to eligible supervised users.

### 6.3 Administrators

Administrators can:

- Review access requests from students and researchers.
- Approve, reject, or revoke user access.
- Create Professor accounts and profiles.
- Register and manage GPU nodes, including draining, returning, and decommissioning nodes.
- Monitor GPU pool status, inspect and acknowledge node alerts, and manage maintenance windows.
- View the administrative ML Flow overview and aggregate experiment statistics.

The exact actions available in the portal depend on the server configuration and the signed-in role.

---


## 7. Optional component: Steward

Steward is Prayog's optional AI-assisted experiment support component. It reads a user's own runs and logs, and gives diagnoses and advice.

Steward is not required for core authentication, access approval, GPU scheduling, or experiment tracking. Prayog can operate without it.

### 7.1 Availability and usage

Users do not need to install or run Steward separately. When an administrator has enabled it (see [Optional: the Steward](#optional-the-steward)), its results appear in the web application's Steward area:

- **Failed run:** a diagnosis of the failure, and possibly a corrected job that the owner may approve.
- **A GPU that fails for two or more users within 24 hours:** a hardware warning for the administrator.
- **Failed rebuild:** a chat about the user's own records, through a single-use link.
- **Successful rebuild:** optimisation advice.

Steward only reads and proposes. It never writes platform records, and a job it suggests runs only after its owner approves it.

### 7.2 Further documentation

For component-specific information, architecture, configuration, and security boundaries, see:

- [Steward README](steward/README.md)
- [Steward Design documentation](steward/DESIGN.md)
- [AI Integration documentation](backend/AI_INTEGRATION.md)

---


## 8. Troubleshooting

| Problem | What to check |
|---|---|
| Cannot connect to the server | Confirm that the server is running and that the configured API URL is correct. |
| Login is rejected | Check your email, role, and password. For CLI login, also confirm that your platform access is currently active. |
| CLI session has expired | CLI login tokens expire after 60 minutes. Run `prayog login` again to start a new session. |
| Access request is pending | Both the supervising Professor and an administrator must complete their required decisions. |
| GPU job remains queued | Check the requested GPU count, walltime, available nodes, quota, and scheduler state. |
| CLI command is unavailable | Run `prayog help` after logging in to see commands available to your role. |
| Project upload fails | Check the folder path, permissions, and contents. Symbolic links are not supported and cause project uploads to fail. |
| Project has no uploaded version | Create a project version using `prayog project link NAME FOLDER` before submitting a run. |
| Dataset is unavailable | Check the supplied `--data` path. Temporary staged copies may have expired and need to be supplied again. |
| Rebuild is rejected | Confirm that the original run completed successfully, has a verified snapshot, and that the supplied dataset matches the original fingerprints. |
| Downloaded artifacts are not saved | Check the destination directory. The CLI verifies downloaded files and does not overwrite existing files. |

For API-level details, open the interactive API documentation at `/docs` on your running server.


---

## 9. Further documentation

The following documents provide additional details about Prayog's CLI, APIs, and individual components.

| Document | Description |
|---|---|
| [CLI README](cli/README.md) | Complete CLI command reference, options, examples, and usage details. |
| [API.md](API.md) | User registration, authentication, access requests, and role-based permissions. |
| [API_GPU.md](API_GPU.md) | GPU job scheduling, node-agent operations, GPU-node administration, and maintenance. |
| [API_STEWARD.md](API_STEWARD.md) | Steward API endpoints and rebuild-related integration routes. |
| [backend/ML_FLOW.md](backend/ML_FLOW.md) | Technical details of project snapshots, datasets, artifacts, experiment runs, and rebuilds. |
| [backend/AI_INTEGRATION.md](backend/AI_INTEGRATION.md) | Steward integration contract, eligibility rules, current implementation status, and remaining integration work. |
| [Steward README](steward/README.md) | Steward component overview and user/developer entry point. |
| [Steward Design](steward/DESIGN.md) | Steward architecture, database boundaries, security model, configuration, and operation. |

---

## Project information

Prayog is developed as part of the CS455 (Introduction to Software Engineering) course at IIT Kanpur.
