"""`prayog project ...`: your ML projects on the Prayog server, their uploaded versions and their runs."""
import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from prayog import folder, restore
from prayog.client import Client
from prayog.context import Context
from prayog.dataset import human
from prayog.errors import CliError, USAGE
from prayog.ui import day, table

MAX_NAME = 120
MAX_DESCRIPTION = 4000
MODEL_NAME = "default"          # a project's folder is stored under one model and one experiment
EXPERIMENT_NAME = "main"


def setup_project(p):
    sub = p.add_subparsers(dest="project_cmd", metavar="<subcommand>", required=True)

    c = sub.add_parser("create", help="create a project on the server",
                       description="Create a project. Its name must be unique among your projects "
                                   "(upper/lower case is ignored). The server gives it a unique project ID.")
    c.add_argument("name", help="project name, up to 120 characters")
    c.add_argument("-d", "--description", help="what the project is for")
    c.set_defaults(func=create)

    c = sub.add_parser("link", help="upload a folder to a project for the first time",
                       description="Upload a folder to a project you created. The server keeps its own copy "
                                   "of the code, configs and seeds, and a fingerprint of each dataset file, "
                                   "so the project can be rebuilt even if the folder changes later. The upload "
                                   "becomes the project's version A. Secrets (.env, keys, credentials) are never read.")
    c.add_argument("name", help="the project's name")
    c.add_argument("folder", help="the folder to upload")
    c.add_argument("--chunk-mib", type=int, default=folder.DEFAULT_CHUNK_MIB, help=argparse.SUPPRESS)
    c.set_defaults(func=link)

    u = sub.add_parser("update", help="upload what changed in a linked folder as a new version",
                       description="Upload a new version (B, C, ...) of a folder that was uploaded with "
                                   "`prayog project link`. Only the parts that changed are sent, and earlier "
                                   "versions are kept. The new version becomes the current one.")
    u.add_argument("folder", nargs="?", default=".", help="the linked folder (default: the current folder)")
    u.add_argument("--chunk-mib", type=int, default=folder.DEFAULT_CHUNK_MIB, help=argparse.SUPPRESS)
    u.set_defaults(func=update)

    v = sub.add_parser("versions", help="list a project's versions",
                       description="List the versions of a project, newest first. 'head' is the newest; "
                                   "'current' is the one `prayog run` uses unless you name another.")
    v.add_argument("name", help="the project's name")
    v.set_defaults(func=versions)

    k = sub.add_parser("current", help="show the version runs use by default")
    k.add_argument("name", help="the project's name")
    k.set_defaults(func=current)

    o = sub.add_parser("checkout", help="make an earlier (or later) version the current one",
                       description="Choose the version `prayog run` uses by default. It does not change or "
                                   "delete any version; `prayog project update` always adds a new one on top.")
    o.add_argument("name", help="the project's name")
    o.add_argument("version", help="a version letter, such as A")
    o.set_defaults(func=checkout)

    r = sub.add_parser("runs", help="list a project's runs", description="List runs, newest first: A run 1, A run 2, B run 1, ...")
    r.add_argument("name", help="the project's name")
    r.add_argument("--version", help="only this version's runs")
    r.set_defaults(func=runs)

    s = sub.add_parser("status", help="show a project's current version and the state of its runs",
                       description="Show the project's current version, and a run's state, metrics and saved "
                                   "checkpoints (the latest run, unless --run names one).")
    s.add_argument("name", help="the project's name")
    s.add_argument("--run", metavar="VERSION/N", help="a run such as A/2 (default: the latest run)")
    s.set_defaults(func=status)

    d = sub.add_parser("download", help="download a run's checkpoints, log and metrics",
                       description="Download what a run saved into a folder: checkpoints/step-N.<ext> (the best "
                                   "one by default), run.log and metrics.json. Every file is checked against the "
                                   "checksum the server recorded, and an existing file is never overwritten.")
    d.add_argument("name", help="the project's name")
    d.add_argument("run", metavar="VERSION/N", help="the run, such as A/1")
    d.add_argument("--to", required=True, metavar="DIR", help="the folder to save into (created if needed)")
    d.add_argument("--checkpoint", default="best", metavar="WHICH",
                   help="best (default; the last one if none is marked best), last, all, none, or a step number")
    d.add_argument("--no-log", action="store_true", help="do not download the log")
    d.add_argument("--no-metrics", action="store_true", help="do not write metrics.json")
    d.set_defaults(func=download)

    m = sub.add_parser("compare", help="check a rebuild against the run it reproduces",
                       description="Compare a rebuild (started with `prayog run NAME --rebuild A/1`) with the run it "
                                   "reproduces: every metric the original logged, within a tolerance, and whether the "
                                   "saved weights are the very same bytes (compared by BLAKE3).")
    m.add_argument("name", help="the project's name")
    m.add_argument("run", metavar="VERSION/N", help="the rebuild, such as A/3")
    m.add_argument("--tolerance", type=float, default=0.0,
                   help="largest allowed absolute difference per metric (default: 0, exactly equal)")
    m.set_defaults(func=compare)

    g = sub.add_parser("pull", help="restore a stored version of a project's folder",
                       description="Download a stored version into a new folder, checking every piece against "
                                   "its recorded checksum. Datasets are not stored; if the version has dataset "
                                   "fingerprints, your local copy is checked against them.")
    g.add_argument("name", help="the project's name")
    g.add_argument("destination", help="a new folder to restore into (must not exist)")
    g.add_argument("--version", help="a version letter (default: the current version)")
    g.add_argument("--data-dir", help="where your datasets are, to check them against the version's fingerprints")
    g.set_defaults(func=pull)

    x = sub.add_parser("clean", help="delete a project's temporary dataset copies now",
                       description="Staged datasets are deleted automatically two days after the run that used "
                                   "them ends. This deletes them now, except any a running job still needs. "
                                   "Run results and the datasets' fingerprints are kept.")
    x.add_argument("name", help="the project's name")
    x.set_defaults(func=clean)


# ── shared lookups ────────────────────────────────────────────────────

def find_project(client: Client, name: str) -> dict:
    wanted = name.strip().lower()
    for project in client.get("/mlflow/projects"):
        if project["name"].lower() == wanted:
            return project
    raise CliError(f"You have no project named '{name}'. Create it first with `prayog project create {name}`.")


def _ensure_experiment(client: Client, project: dict) -> int:
    """The experiment that holds the project's folder, created with its model on first use."""
    for experiment in client.get("/mlflow/experiments"):
        if experiment["project_id"] == project["project_id"] and experiment["name"] == EXPERIMENT_NAME:
            return experiment["id"]
    models = client.get(f"/mlflow/projects/{project['project_id']}/models")
    model = next((m for m in models if m["name"] == MODEL_NAME), None)
    if model is None:
        model = client.post(f"/mlflow/projects/{project['project_id']}/models", json={"name": MODEL_NAME})
    created = client.post("/mlflow/experiments", json={
        "project_id": project["project_id"], "model_id": model["model_id"], "name": EXPERIMENT_NAME})
    return created["id"]


def _version_table(items: list) -> list:
    return [[i["version"], "current" if i["is_current"] else "", "head" if i["is_head"] else "",
             str(i["file_count"]), str(i["run_count"]), day(i["created_at"]), i["manifest_hash"][:12]] for i in items]


def _no_version(name: str) -> CliError:
    return CliError(f"'{name}' has no uploaded version yet. Upload a folder with `prayog project link {name} FOLDER`.")


# ── create, link, update ──────────────────────────────────────────────

def create(ctx: Context, args) -> None:
    name = args.name.strip()
    if not name:
        raise CliError("Project name cannot be blank.", USAGE)
    if len(name) > MAX_NAME:
        raise CliError(f"Project name is too long ({len(name)} characters; the limit is {MAX_NAME}).", USAGE)
    if args.description is not None and len(args.description) > MAX_DESCRIPTION:
        raise CliError(f"Description is too long (the limit is {MAX_DESCRIPTION} characters).", USAGE)

    project = ctx.client().post("/mlflow/projects", json={"name": name, "description": args.description})
    print(f"Created project '{project['name']}'.")
    print(f"Project ID: {project['project_id']}")


def _upload(client: Client, root: Path, project: dict, experiment_id: int, link: dict, chunk_mib: int) -> None:
    try:
        files = folder.inventory(root)
        fingerprint = folder.inventory_fingerprint(files)
        versions = link["versions"]
        if versions and versions[-1]["inventory_sha256"] == fingerprint:
            print(f"Nothing changed since version {versions[-1]['version']}; nothing uploaded.")
            return
        print(f"Found {len(files)} files; uploading what the server does not have yet...")
        result = folder.upload_folder(client, experiment_id, root, files, chunk_mib)
    except (ValueError, OSError) as exc:
        raise CliError(f"{exc} Nothing was recorded.", USAGE) from exc
    registered = client.post(f"/mlflow/projects/{project['project_id']}/versions",
                             json={"manifest_artifact_id": result["manifest_artifact_id"]})
    label = registered["version"]
    versions.append({"version": label, "manifest_artifact_id": result["manifest_artifact_id"],
                     "inventory_sha256": fingerprint, "file_count": result["file_count"]})
    link.update({"project_id": project["project_id"], "project_name": project["name"], "experiment_id": experiment_id})
    folder.write_link(root, link)
    if registered["created"]:
        print(f"Uploaded version {label} of '{project['name']}'; it is now the current version.")
    else:
        print(f"This folder matches version {label}, which was already stored; it is now the current version.")
    print(f"  files: {result['file_count']} ({result['data_fingerprints']} dataset files fingerprinted, not uploaded)")
    print(f"  pieces sent: {result['uploaded_chunks']}, already on the server: {result['reused_chunks']}")


def link(ctx: Context, args) -> None:
    root = folder.check_folder(Path(args.folder).expanduser())
    if folder.read_link(root) is not None:
        raise CliError("This folder is already linked. Use `prayog project update` to upload changes.", USAGE)
    client = ctx.client()
    project = find_project(client, args.name)
    _upload(client, root, project, _ensure_experiment(client, project), {"versions": []}, args.chunk_mib)


def update(ctx: Context, args) -> None:
    root = folder.check_folder(Path(args.folder).expanduser())
    existing = folder.read_link(root)
    if existing is None:
        raise CliError("This folder is not linked to a project yet. Run `prayog project link NAME FOLDER` first.", USAGE)
    client = ctx.client()
    project = {"project_id": existing["project_id"], "name": existing.get("project_name", existing["project_id"])}
    _upload(client, root, project, existing["experiment_id"], existing, args.chunk_mib)


# ── versions ──────────────────────────────────────────────────────────

def versions(ctx: Context, args) -> None:
    client = ctx.client()
    project = find_project(client, args.name)
    items = client.get(f"/mlflow/projects/{project['project_id']}/versions")
    if not items:
        raise _no_version(project["name"])
    print(table(["VERSION", "", "", "FILES", "RUNS", "UPLOADED", "ID"], _version_table(items)))


def current(ctx: Context, args) -> None:
    client = ctx.client()
    project = find_project(client, args.name)
    items = client.get(f"/mlflow/projects/{project['project_id']}/versions")
    shown = next((i for i in items if i["is_current"]), None)
    if shown is None:
        raise _no_version(project["name"])
    note = "the newest version" if shown["is_head"] else f"older than the newest, {items[0]['version']}"
    print(f"Current version of '{project['name']}': {shown['version']} ({note}).")


def checkout(ctx: Context, args) -> None:
    client = ctx.client()
    project = find_project(client, args.name)
    shown = client.put(f"/mlflow/projects/{project['project_id']}/current", json={"version": args.version})
    note = "the newest version" if shown["is_head"] else "not the newest version"
    print(f"'{project['name']}' now uses version {shown['version']} ({note}) for new runs.")


# ── runs and status ───────────────────────────────────────────────────

def describe_run(run: dict) -> list:
    lines = [f"{run['label']}: {run['status']}" + (f" ({run['job_state']})" if run["job_state"] != run["status"] else "")
             + (f", a rebuild of {run['rebuild_of']}" if run.get("rebuild_of") else "")]
    if run.get("queue_reason"):
        lines[0] += f", waiting: {run['queue_reason']}"
    lines.append(f"  {run['gpus']} GPU(s), walltime {run['walltime']}")
    if run["started_at"]:
        lines.append(f"  started {run['started_at'][:19].replace('T', ' ')}" +
                     (f", ended {run['finished_at'][:19].replace('T', ' ')}" if run["finished_at"] else ""))
    if run["metrics"]:
        lines.append("  metrics (last value): " + ", ".join(f"{k}={v:g}" for k, v in sorted(run["metrics"].items())))
    if run["checkpoints"]:
        best = next((c["step"] for c in run["checkpoints"] if c["is_best"]), None)
        lines.append(f"  checkpoints: {len(run['checkpoints'])} saved (steps {run['checkpoints'][0]['step']}"
                     f" to {run['checkpoints'][-1]['step']})" + (f", best at step {best}" if best is not None else ""))
    if run["dataset"]:
        staged = sum(1 for d in run["dataset"] if d["staged"])
        size = sum(d["size_bytes"] for d in run["dataset"])
        lines.append(f"  dataset: {len(run['dataset'])} files ({human(size)}), "
                     + (f"{staged} still staged" if staged else "temporary copy deleted; fingerprint kept"))
    if run["error_summary"]:
        lines.append(f"  {run['error_summary']}")
    if run["snapshot_id"]:
        lines.append(f"  sealed as snapshot {run['snapshot_id']}")
    return lines


def runs(ctx: Context, args) -> None:
    client = ctx.client()
    project = find_project(client, args.name)
    items = client.get(f"/mlflow/projects/{project['project_id']}/runs", params={"version": args.version} if args.version else None)
    if not items:
        print(f"'{project['name']}' has no runs yet. Start one with `prayog run {project['name']} --gpus 1 --walltime 01:00`.")
        return
    print(table(["RUN", "STATUS", "GPUS", "WALLTIME", "CHECKPOINTS", "SUBMITTED"],
                [[r["label"], r["status"], str(r["gpus"]), r["walltime"], str(len(r["checkpoints"])), day(r["created_at"])]
                 for r in items]))


def status(ctx: Context, args) -> None:
    client = ctx.client()
    project = find_project(client, args.name)
    base = f"/mlflow/projects/{project['project_id']}"
    summary = client.get(f"{base}/status")
    if summary["current_version"] is None:
        raise _no_version(project["name"])
    print(f"Project '{summary['name']}': current version {summary['current_version']}"
          + (f" (newest is {summary['head_version']})" if summary["current_version"] != summary["head_version"] else "")
          + f", {summary['versions']} version(s), {summary['runs']} run(s).")
    if args.run:
        version, _, number = args.run.partition("/")
        if not number.isdigit():
            raise CliError("Name a run like A/2 (version letter, then run number).", USAGE)
        run = client.get(f"{base}/runs/{version}/{int(number)}")
    else:
        run = summary["latest_run"]
    if run is None:
        print("No runs yet.")
    else:
        print("\n".join(describe_run(run)))
    if summary["staged_files"]:
        print(f"Temporary dataset copies held: {summary['staged_files']} files ({human(summary['staged_bytes'])}); "
              "deleted two days after their last run ends, or now with `prayog project clean`.")


def compare(ctx: Context, args) -> None:
    version, _, number = args.run.partition("/")
    if not number.isdigit():
        raise CliError("Name the rebuild like A/3 (version letter, then run number).", USAGE)
    if not args.tolerance >= 0:
        raise CliError("--tolerance must be zero or more.", USAGE)
    client = ctx.client()
    project = find_project(client, args.name)
    result = client.get(f"/mlflow/projects/{project['project_id']}/runs/{version}/{int(number)}/compare",
                        params={"tolerance": args.tolerance})
    print(f"{result['rebuilt']} ({result['rebuilt_status']}) against {result['original']}, tolerance {result['tolerance']:g}:")
    if result["metrics"]:
        rows = [[m["name"], str(m["step"]), f"{m['original']:g}", "-" if m["rebuilt"] is None else f"{m['rebuilt']:g}",
                 "-" if m["difference"] is None else f"{m['difference']:g}", "ok" if m["within_tolerance"] else "DIFFERS"]
                for m in result["metrics"]]
        print(table(["METRIC", "STEP", "ORIGINAL", "REBUILT", "DIFFERENCE", ""], rows))
    else:
        print("  The original logged no metrics, so none can be compared.")
    weights = result["weights"]
    if weights["original_blake3"] is None and weights["rebuilt_blake3"] is None:
        print("Weights: neither run saved a checkpoint.")
    else:
        print("Weights: " + ("identical, byte for byte (BLAKE3 " + weights["rebuilt_blake3"][:12] + ")" if weights["identical"]
                              else "different"))
    print("Result: " + ("REPRODUCED" if result["reproduced"] else "NOT reproduced")
          + (" (metrics match, weights differ: expected unless the run is fully deterministic)"
             if result["reproduced"] and not weights["identical"] else ""))


def _save_artifact(client: Client, known: dict, artifact_id: str, destination: Path) -> None:
    """Stream one artifact to `destination`, checking its size and SHA-256 against the server's record."""
    recorded = known.get(artifact_id)
    if recorded is None:
        raise CliError("The server has no record of that file.")
    if destination.exists() or destination.is_symlink():
        raise CliError(f"'{destination}' already exists; it is never overwritten. Choose another folder with --to.", USAGE)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".part")
    try:
        with client.stream(f"/mlflow/artifacts/{artifact_id}/content") as response, os.fdopen(handle, "wb") as out:
            for block in response.iter_bytes():
                size += len(block)
                digest.update(block)
                out.write(block)
        if size != recorded["size_bytes"] or digest.hexdigest() != recorded["sha256"]:
            raise CliError(f"{destination.name} failed its checksum, so it was not saved.")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _wanted_checkpoints(checkpoints: list, which: str) -> list:
    which = which.strip().lower()
    if which == "none" or not checkpoints:
        return []
    if which == "all":
        return checkpoints
    if which == "last":
        return [checkpoints[-1]]
    if which == "best":
        return [next((c for c in checkpoints if c["is_best"]), checkpoints[-1])]
    if which.isdigit():
        chosen = [c for c in checkpoints if c["step"] == int(which)]
        if not chosen:
            raise CliError(f"The run has no checkpoint at step {which}. Its steps are: "
                           + ", ".join(str(c["step"]) for c in checkpoints) + ".", USAGE)
        return chosen
    raise CliError("--checkpoint must be best, last, all, none or a step number.", USAGE)


def download(ctx: Context, args) -> None:
    version, _, number = args.run.partition("/")
    if not number.isdigit():
        raise CliError("Name the run like A/1 (version letter, then run number).", USAGE)
    target = Path(args.to).expanduser()
    client = ctx.client()
    project = find_project(client, args.name)
    base = f"/mlflow/projects/{project['project_id']}"
    run = client.get(f"{base}/runs/{version}/{int(number)}")
    wanted = _wanted_checkpoints(run["checkpoints"], args.checkpoint)
    owner = next((v for v in client.get(f"{base}/versions") if v["version"] == run["version"]), None)
    known = {a["id"]: a for a in client.get(f"/mlflow/experiments/{owner['experiment_id']}/artifacts")} if owner else {}
    saved = []
    for item in wanted:
        name = f"step-{item['step']}" + ("-best" if item["is_best"] else "")
        _save_artifact(client, known, item["artifact_id"], target / "checkpoints" / name)
        saved.append(f"checkpoints/{name}")
    if not args.no_log:
        if run["logs_artifact_id"]:
            _save_artifact(client, known, run["logs_artifact_id"], target / "run.log")
            saved.append("run.log")
        else:
            print("This run has no log yet.")
    if not args.no_metrics:
        rows = client.get(f"/mlflow/runs/{run['run_id']}/metrics")
        path = target / "metrics.json"
        if path.exists():
            raise CliError(f"'{path}' already exists; it is never overwritten. Choose another folder with --to.", USAGE)
        target.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{"name": r["name"], "step": r["step"], "value": r["value"]} for r in rows],
                                   indent=2) + "\n", encoding="utf-8")
        saved.append(f"metrics.json ({len(rows)} values)")
    print(f"Saved {run['label']} to {target}:")
    for entry in saved or ["(nothing selected)"]:
        print(f"  {entry}")


def clean(ctx: Context, args) -> None:
    client = ctx.client()
    project = find_project(client, args.name)
    result = client.delete(f"/mlflow/projects/{project['project_id']}/staged-data")
    print(f"Deleted {result['deleted_files']} staged file(s), freeing {human(result['freed_bytes'])}.")
    if result["kept_in_use"]:
        print(f"Kept {result['kept_in_use']} file(s) that a job still needs.")


# ── pull ──────────────────────────────────────────────────────────────

def pull(ctx: Context, args) -> None:
    client = ctx.client()
    project = find_project(client, args.name)
    items = client.get(f"/mlflow/projects/{project['project_id']}/versions")
    if not items:
        raise _no_version(project["name"])
    wanted: Optional[str] = args.version.strip().upper() if args.version else None
    chosen = next((i for i in items if (i["version"] == wanted if wanted else i["is_current"])), None)
    if chosen is None:
        known = ", ".join(i["version"] for i in reversed(items))
        raise CliError(f"'{project['name']}' has no version {wanted}. Its versions are: {known}.", USAGE)
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
    try:
        result = restore.pull_version(client, chosen["experiment_id"], chosen["manifest_artifact_id"],
                                      Path(args.destination).expanduser(), data_dir)
    except (ValueError, OSError) as exc:
        raise CliError(f"{exc} Nothing was restored.", USAGE) from exc
    print(f"Restored version {chosen['version']} to {args.destination} ({result['restored_files']} files).")
    for path in result["data_fingerprints"]:
        print(f"  dataset checked, not copied: {path}")
