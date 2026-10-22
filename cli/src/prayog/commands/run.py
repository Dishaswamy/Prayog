"""`prayog run NAME`: run a project's version on the GPUs."""
import json
import time
import uuid
from pathlib import Path

from prayog import dataset
from prayog.commands.project import find_project
from prayog.context import Context
from prayog.errors import CliError, SERVER_UNREACHABLE, USAGE

SUBMIT_ATTEMPTS = 3


def setup_run(p):
    p.description = ("Queue a run of a project's current version (or --version) on the GPU cluster. The files given "
                     "with --data are uploaded for the run (only those the server lacks) and deleted automatically "
                     "two days after the run ends; the run keeps their fingerprints. Results (metrics, checkpoints, "
                     "log) are saved as they are produced: see `prayog project status`.")
    p.add_argument("name", help="the project's name")
    p.add_argument("--gpus", type=int, default=1, help="number of GPUs on one node (default: 1)")
    p.add_argument("--walltime", required=True, metavar="HH:MM", help="the longest the run may take, such as 04:00")
    p.add_argument("--version", help="a version letter (default: the project's current version)")
    p.add_argument("--data", action="append", default=[], metavar="PATH",
                   help="a dataset file or folder to upload for this run (repeatable)")
    p.add_argument("--entrypoint", help="the Python file to run (default: main.py)")
    p.add_argument("--hp", action="append", default=[], metavar="KEY=VALUE",
                   help="a hyperparameter, e.g. --hp lr=0.01 --hp epochs=20 (repeatable; the value is read as JSON if it can be)")
    p.add_argument("--rebuild", metavar="VERSION/N",
                   help="reproduce a finished run, such as A/1: the same version, entrypoint and hyperparameters, "
                        "and the same data, which must match the original's recorded fingerprint. Compare the "
                        "result with `prayog project compare`")
    p.set_defaults(func=run)


def parse_hyperparameters(items: list) -> dict:
    result: dict = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise CliError(f"'{item}' is not KEY=VALUE.", USAGE)
        try:
            result[key.strip()] = json.loads(value)
        except ValueError:
            result[key.strip()] = value
    return result


def run(ctx: Context, args) -> None:
    if args.gpus < 1:
        raise CliError("--gpus must be at least 1.", USAGE)
    if args.rebuild and (args.version or args.entrypoint or args.hp):
        raise CliError("A rebuild uses the original run's version, entrypoint and hyperparameters; "
                       "drop --version, --entrypoint and --hp.", USAGE)
    hyperparameters = parse_hyperparameters(args.hp)
    files = dataset.combine([Path(p).expanduser() for p in args.data]) if args.data else []
    client = ctx.client()
    project = find_project(client, args.name)
    base = f"/mlflow/projects/{project['project_id']}"

    if files:
        print(f"Dataset: {len(files)} file(s), {dataset.human(sum(f.size_bytes for f in files))}.")
        sent, there = dataset.stage(client, files, say=print)
        print(f"  uploaded {sent}, already on the server {there}.")

    body = {"gpus": args.gpus, "walltime": args.walltime, "client_request_id": uuid.uuid4().hex,
            "dataset": [{"path": f.path, "size_bytes": f.size_bytes, "blake3": f.blake3} for f in files]}
    if args.rebuild:
        body["rebuild_of"] = args.rebuild
    else:
        body.update({"entrypoint": args.entrypoint or "main.py", "hyperparameters": hyperparameters})
        if args.version:
            body["version"] = args.version
    for attempt in range(SUBMIT_ATTEMPTS):                       # same request ID each time: a retry never double-submits
        try:
            queued = client.post(f"{base}/runs", json=body)
            break
        except CliError as exc:
            if exc.code != SERVER_UNREACHABLE or attempt == SUBMIT_ATTEMPTS - 1:
                raise
            time.sleep(1 + attempt)
    note = f", a rebuild of {queued['rebuild_of']}" if queued.get("rebuild_of") else ""
    print(f"Queued {queued['label']}{note} (job {queued['job_id']}): {queued['gpus']} GPU(s), walltime {queued['walltime']}.")
    print(f"Follow it with `prayog project status {project['name']}`."
          + (f" When it finishes, `prayog project compare {project['name']} {queued['version']}/{queued['run_number']}` "
             "checks it against the original." if args.rebuild else ""))
