# Prayog

**Prayog is a GPU-computing platform for IIT Kanpur students and researchers.** It provides a web portal and command-line client to request GPU access, manage ML projects, submit experiments, monitor runs, and reproduce results.

This README focuses on **how to get started and use Prayog**. Architecture and implementation details are kept in the linked technical documents.

## What can I do with Prayog?

- **Request GPU access:** submit an access request and get approval from your supervising Professor and an administrator.
- **Use the GPU pool:** submit jobs with a requested GPU count and walltime. The scheduler allocates available resources according to platform policies.
- **Manage ML projects:** upload code as versioned snapshots, attach datasets to runs, and track experiments.
- **Monitor and retrieve results:** view run status, logs, metrics and checkpoints.
- **Reproduce experiments:** rebuild a previous run using its recorded code version, settings and dataset fingerprints.
- **Get AI-assisted help:** the optional Steward can diagnose eligible failed runs and provide guidance.

## Choose how you want to use Prayog

| If you are… | Start here |
|---|---|
| A student or researcher | [Web portal and first steps](#1-open-the-web-portal) |
| Running experiments from your terminal | [Install and use the CLI](#2-use-the-command-line-client) |
| A Professor | [Professor workflow](#professor-workflow) |
| An administrator or developer | [Run Prayog locally](#run-prayog-locally) |

## 1. Open the web portal

When the Prayog server is running, open:

- **Web application:** `http://127.0.0.1:8000/`
- **Interactive API documentation:** `http://127.0.0.1:8000/docs`
- **Health check:** `http://127.0.0.1:8000/health`

The root URL redirects to the application at `/app/`.

### Students and researchers

1. Register using your IITK email address, or sign in if you already have an account.
2. Open the GPU access section and submit an access request. Select the appropriate purpose and provide the requested academic and Professor details.
3. Wait for **both** your supervising Professor and an administrator to approve the request.
4. Once access is active, use the dashboard to view your projects, versions, runs, quota and experiment results.
5. Use the CLI for uploading project folders, submitting runs, downloading artifacts and rebuilding experiments.

Your access request is not the same as an active GPU allocation. You must have approved access before submitting work.

### Professor workflow

Professors can review access requests addressed to them and make an approval or rejection decision. The Professor workspace also provides read-only views of the experiment records for the students and researchers they supervise, along with supported handover workflows.

### Administrator workflow

Administrators can review access requests, manage Professor accounts and GPU nodes, inspect the pool and audit information, and access administrative experiment-ledger views.

The exact actions visible in the portal depend on the signed-in role and the server configuration.

## 2. Use the command-line client

The `prayog` CLI is the recommended way to upload code and manage the ML experiment lifecycle.

### Install

From a clone of this repository:

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\\Scripts\\Activate.ps1

python -m pip install -e ./cli
```

The CLI requires Python 3.9 or newer. It communicates with a running Prayog server; installing it does not create an account or grant GPU access.

### Sign in

```bash
prayog login
```

Follow the prompts for your IITK email, role and password. To specify them directly:

```bash
prayog login -e you@iitk.ac.in -r student
```

The CLI stores an access token locally, not your password. Use `prayog whoami` to check the active account and `prayog logout` to end the session.

If the server is not running at `http://localhost:8000`, set its URL when logging in:

```bash
prayog login --api-url https://YOUR-PRAYOG-SERVER
```

### Typical student / researcher workflow

Run these commands from your project workspace. Replace the example project name and paths with your own.

```bash
# 1. Create a project on Prayog
prayog project create vision

# 2. Upload your code folder as the first version (A)
prayog project link vision ./my-training-code

# 3. Submit an experiment
prayog run vision --gpus 2 --walltime 04:00 --data ./data --hp lr=0.01

# 4. Check the run
prayog project status vision

# 5. List previous runs
prayog project runs vision

# 6. Download results from a completed run
prayog project download vision A/1 --to ./results

# 7. Rebuild a previous run (provide the same dataset)
prayog run vision --rebuild A/1 --walltime 04:00 --data ./data

# 8. Compare the rebuild with the original
prayog project compare vision A/2
```

A project contains uploaded code versions and the runs created from those versions. When you change your code, upload the changes with:

```bash
prayog project update
```

Useful commands:

| Command | Purpose |
|---|---|
| `prayog help` | Show commands available to your account |
| `prayog help project` | Show help for a command group |
| `prayog project versions NAME` | List uploaded versions |
| `prayog project current NAME` | Show the current version |
| `prayog project checkout NAME VERSION` | Select a version for future runs |
| `prayog project status NAME` | Inspect project and run status |
| `prayog project runs NAME` | List runs |
| `prayog project download NAME VERSION/RUN --to DIR` | Download run results |
| `prayog project compare NAME VERSION/RUN` | Compare a rebuild with its original |

For the full command reference, options, data handling rules and examples, see **[CLI README](cli/README.md)**.

### Important notes about experiment data

- `project link` and `project update` upload project code and configuration as versioned content.
- Dataset files are fingerprinted when a project is linked; provide the actual dataset to a run with `--data`.
- A run records its code version, hyperparameters and dataset fingerprints.
- The CLI verifies downloaded artifacts. Do not assume a rebuild can use a dataset that has been deleted from your local machine: provide the original data again.
- Keep credentials, API keys and private data out of project folders. The CLI excludes common secret files and build artifacts, but you should still review what you are uploading.

## Professor workflow

1. Sign in to the web portal with your Professor account.
2. Open the access-request workspace to review requests assigned to you.
3. Inspect the request details and approve or reject it.
4. Use the experiment-ledger views to inspect eligible records from supervised users.
5. Review and approve or reject project handover requests where applicable.

An access request becomes active only after the required Professor and administrator decisions have both been recorded.

## Run Prayog locally

These instructions are for developers setting up a local instance. They are not needed by a user connecting to an already-running server.

### Prerequisites

- Python 3.11 or newer (the project is tested with Python 3.12/3.13).
- Docker and Docker Compose.
- Git.

### 1. Clone the repository and install dependencies

```bash
git clone https://github.com/Dishaswamy/Prayog.git
cd Prayog

python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\\Scripts\\Activate.ps1

python -m pip install -r backend/requirements.txt -e ./steward
```

Install the CLI separately if you want to try it from this checkout:

```bash
python -m pip install -e ./cli
```

### 2. Configure the environment

```bash
# Linux / macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

Edit `.env`. At minimum, configure the PostgreSQL credentials, a strong `JWT_SECRET_KEY`, and a separate `STEWARD_DB_PASSWORD`. See [`.env.example`](.env.example) for the available settings. Never commit your real `.env` file.

### 3. Start PostgreSQL and initialise the database

```bash
docker compose up -d postgres

cd backend
python -m alembic upgrade head
python -m scripts.setup_steward
```

Skip the Steward setup step only if you are intentionally running without the Steward and have configured it accordingly.

### 4. Create development accounts and sample nodes

For a disposable local development database:

```bash
python -m scripts.seed_dev
```

This creates demo accounts and sample GPU nodes and prints the commands needed to start their simulated node agents. The demo passwords are public and must **never** be used on a shared or production deployment.

To create only the first administrator instead:

```bash
python -m scripts.create_admin admin@iitk.ac.in "REPLACE_WITH_A_STRONG_PASSWORD"
```

### 5. Start the services

Keep each process running in its own terminal. Run these commands from the `backend/` directory with the virtual environment active.

```bash
# Terminal 1: API and web application
uvicorn main:app --host 127.0.0.1 --port 8000

# Terminal 2: GPU scheduler
python -m logic.gpu.scheduler.run

# Terminal 3: Steward worker (optional)
python -m integration.steward_worker

# Terminal 4: Steward Runtime (optional)
uvicorn steward_runtime.app:app --host 127.0.0.1 --port 8100 --env-file ../.env
```

The default local node agent is simulated. Start the agent using the command and one-time enrolment token printed by `seed_dev`. The simulated agent lets you exercise the workflow without attaching a physical GPU to your development machine.

Check the API:

```bash
curl http://127.0.0.1:8000/health
```

A healthy local instance returns a response indicating that both the API and database are available.

## Troubleshooting

| Problem | What to check |
|---|---|
| Cannot connect to the server | Confirm the API is running and that `PRAYOG_API_URL` or `--api-url` points to the correct address. |
| Login is rejected | Check the email, role and password. For students/researchers, confirm that access has been approved and is currently active. |
| Access request is pending | Both the supervising Professor and an administrator must decide the request. |
| Job remains queued | Check the requested GPU count, walltime, available nodes, quota and scheduler state. |
| CLI command is unavailable | Run `prayog help`; available commands depend on your role and login state. |
| Local API reports a database error | Confirm the PostgreSQL container is running and that the database settings in `.env` match it. |

For endpoint-level details, open the interactive API docs at `/docs` or see [API.md](API.md) and [API_STEWARD.md](API_STEWARD.md).

## Documentation

| Document | What it covers |
|---|---|
| [CLI README](cli/README.md) | Complete CLI command reference and experiment workflow |
| [API.md](API.md) | Authentication, access requests and platform API |
| [API_GPU.md](API_GPU.md) | GPU scheduling and node-agent API |
| [API_STEWARD.md](API_STEWARD.md) | Steward and rebuild-related API |
| [backend/ML_FLOW.md](backend/ML_FLOW.md) | Experiment Ledger, artifacts, snapshots, runs and rebuild worker |
| [backend/AI_INTEGRATION.md](backend/AI_INTEGRATION.md) | Steward / ledger integration contracts |
| [steward/README.md](steward/README.md) | Steward component entry point |
| [steward/DESIGN.md](steward/DESIGN.md) | Steward design, security model and implementation |

## Project information

Prayog is an ongoing CS455 (Introduction to Software Engineering) project at IIT Kanpur. Some workflows depend on the server deployment and configured node agents; the local development setup uses sample accounts and simulated agents. Consult the linked component documentation for implementation details and current limitations.
