import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

MAX_ARTIFACT_BYTES = 250 * 1024 * 1024


def json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"Invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("Expected a JSON object")
    return parsed


def detected_environment() -> dict[str, str]:
    return {
        "os_name": os.name,
        "os_platform": platform.platform(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "runtime_metadata_source": "prayog-mlflow-cli",
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Prayog ML Flow API client")
    root.add_argument("--api-url", default=os.getenv("PRAYOG_API_URL", "http://localhost:8000"))
    root.add_argument("--token", default=os.getenv("PRAYOG_TOKEN"), help="Defaults to PRAYOG_TOKEN; avoid passing secrets in shell history")
    commands = root.add_subparsers(dest="command", required=True)

    project = commands.add_parser("create-project")
    project.add_argument("--name", required=True)
    project.add_argument("--description")

    model = commands.add_parser("create-model")
    model.add_argument("--project-id", required=True)
    model.add_argument("--name", required=True)
    model.add_argument("--description")

    experiment = commands.add_parser("create-experiment")
    experiment.add_argument("--project-id", required=True)
    experiment.add_argument("--model-id", required=True)
    experiment.add_argument("--name", required=True)
    experiment.add_argument("--description")
    experiment.add_argument("--tags-json", type=json_object, default={})

    run = commands.add_parser("create-run")
    run.add_argument("--experiment-id", type=int, required=True)
    run.add_argument("--hyperparameters-json", type=json_object, default={})
    run.add_argument("--environment-json", type=json_object, default={})

    start = commands.add_parser("start-run")
    start.add_argument("--run-id", type=int, required=True)
    start.add_argument("--environment-json", type=json_object)
    start.add_argument("--code-artifact-id")
    start.add_argument("--weights-artifact-id")

    upload = commands.add_parser("upload-artifact")
    upload.add_argument("--experiment-id", type=int, required=True)
    upload.add_argument("--kind", choices=["CODE", "WEIGHTS", "LOG"], required=True)
    upload.add_argument("--file", type=Path, required=True)

    metric = commands.add_parser("metric")
    metric.add_argument("--run-id", type=int, required=True)
    metric.add_argument("--name", required=True)
    metric.add_argument("--value", type=float, required=True)
    metric.add_argument("--step", type=int, default=0)

    finish = commands.add_parser("finish-run")
    finish.add_argument("--run-id", type=int, required=True)
    finish.add_argument("--experiment-id", type=int, required=True)
    finish.add_argument("--status", choices=["SUCCEEDED", "FAILED"], required=True)
    finish.add_argument("--code-file", type=Path)
    finish.add_argument("--weights-file", type=Path)
    finish.add_argument("--logs-file", type=Path)
    finish.add_argument("--error-summary")

    snapshots = commands.add_parser("list-snapshots")
    snapshots.add_argument("--experiment-id", type=int, required=True)

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--snapshot-id", required=True)

    run_status = commands.add_parser("run-status")
    run_status.add_argument("--run-id", type=int, required=True)

    rebuild_status = commands.add_parser("rebuild-status")
    rebuild_status.add_argument("--rebuild-id", type=int, required=True)

    sync = commands.add_parser("sync-results")
    sync.add_argument("--run-id", type=int, required=True)

    download = commands.add_parser("download-artifact")
    download.add_argument("--artifact-id", required=True)
    download.add_argument("--output", type=Path, required=True)

    rebuild = commands.add_parser("rebuild")
    rebuild.add_argument("--snapshot-id", required=True)
    rebuild.add_argument("--environment-json", type=json_object, default={})

    finish_rebuild = commands.add_parser("finish-rebuild")
    finish_rebuild.add_argument("--rebuild-id", type=int, required=True)
    finish_rebuild.add_argument("--state", choices=["REBUILD_SUCCESS", "REBUILD_FAILED", "REBUILD_CANCELLED"], required=True)
    finish_rebuild.add_argument("--logs-file", type=Path)
    finish_rebuild.add_argument("--weights-file", type=Path)
    finish_rebuild.add_argument("--failure-summary")
    return root


def upload(client: httpx.Client, experiment_id: int, kind: str, path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"Artifact must be between 1 byte and {MAX_ARTIFACT_BYTES} bytes")
    with path.open("rb") as source:
        response = client.post(
            f"/mlflow/experiments/{experiment_id}/artifacts",
            params={"kind": kind},
            content=source,
            headers={"Content-Type": "application/octet-stream"},
        )
    response.raise_for_status()
    return response.json()["id"]



def download_artifact_to_path(
    client: httpx.Client,
    artifact_id: str,
    output: Path,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> dict[str, Any]:
    if not output.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {output.parent}")
    if output.is_symlink():
        raise ValueError(f"Refusing to overwrite a symbolic link: {output}")
    temporary_path = None
    total = 0
    digest = hashlib.sha256()
    try:
        with client.stream("GET", f"/mlflow/artifacts/{artifact_id}/content") as response:
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{output.name}.", suffix=".part",
                dir=output.parent, delete=False,
            ) as target:
                temporary_path = Path(target.name)
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_ARTIFACT_BYTES:
                        raise ValueError("Downloaded artifact exceeds the 250 MiB limit")
                    digest.update(chunk)
                    target.write(chunk)
        actual_digest = digest.hexdigest()
        if expected_size is not None and total != expected_size:
            raise ValueError("Downloaded artifact size does not match server metadata")
        if expected_sha256 is not None and actual_digest != expected_sha256:
            raise ValueError("Downloaded artifact checksum does not match server metadata")
        if output.exists() and output.is_file():
            existing_digest = hashlib.sha256()
            existing_size = 0
            with output.open("rb") as existing:
                for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                    existing_digest.update(chunk)
                    existing_size += len(chunk)
            if existing_size == total and existing_digest.hexdigest() == actual_digest:
                temporary_path.unlink(missing_ok=True)
                temporary_path = None
                return {"path": str(output), "size_bytes": total, "unchanged": True}
        os.replace(temporary_path, output)
        temporary_path = None
        os.chmod(output, 0o600)
        return {"path": str(output), "size_bytes": total, "unchanged": False}
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_text_if_changed(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.is_symlink():
        raise ValueError(f"Refusing to overwrite a symbolic link: {path}")
    if path.exists() and path.is_file() and path.read_bytes() == encoded:
        return False
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".part",
            dir=path.parent, delete=False,
        ) as target:
            temporary_path = Path(target.name)
            target.write(encoded)
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
        return True
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def sync_results(client: httpx.Client, run_id: int) -> dict[str, Any]:
    if run_id < 1:
        raise ValueError("Run ID must be a positive integer")
    run_response = client.get(f"/mlflow/runs/{run_id}")
    run_response.raise_for_status()
    run = run_response.json()
    if run.get("id") != run_id:
        raise ValueError("Server returned a different run ID")

    artifact_ids = {
        "weights": run.get("weights_artifact_id"),
        "logs": run.get("logs_artifact_id"),
    }
    artifact_metadata = {}
    if any(artifact_ids.values()):
        artifacts_response = client.get(
            f"/mlflow/experiments/{run['experiment_id']}/artifacts"
        )
        artifacts_response.raise_for_status()
        artifacts_by_id = {
            item["id"]: item for item in artifacts_response.json()
        }
        for artifact_id in artifact_ids.values():
            if artifact_id and artifact_id not in artifacts_by_id:
                raise ValueError("A run output artifact is unavailable to the authenticated owner")
        artifact_metadata = artifacts_by_id

    results_root = Path("results")
    if results_root.is_symlink():
        raise ValueError("Refusing to use a symbolic-link results directory")
    results_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(results_root, 0o700)
    result_dir = results_root / str(run_id)
    if result_dir.is_symlink():
        raise ValueError("Refusing to use a symbolic-link run results directory")
    result_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(result_dir, 0o700)

    synced = []
    for key, filename in (("weights", "weights.bin"), ("logs", "logs.log")):
        artifact_id = artifact_ids[key]
        if not artifact_id:
            continue
        item = artifact_metadata[artifact_id]
        download = download_artifact_to_path(
            client,
            artifact_id,
            result_dir / filename,
            expected_sha256=item["sha256"],
            expected_size=item["size_bytes"],
        )
        synced.append({
            "kind": item["kind"],
            "artifact_id": artifact_id,
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "path": filename,
            "unchanged": download["unchanged"],
        })

    metadata = {
        "run_id": run_id,
        "experiment_id": run["experiment_id"],
        "project_id": run["project_id"],
        "model_id": run["model_id"],
        "run_type": run["run_type"],
        "source_snapshot_id": run.get("source_snapshot_id"),
        "status": run["status"],
        "logging_state": run["logging_state"],
        "logging_warnings": run.get("logging_warnings", []),
        "error_summary": run.get("error_summary"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "artifacts": synced,
    }
    metadata_path = result_dir / "metadata.json"
    metadata_changed = _write_text_if_changed(
        metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\\n"
    )
    return {
        "run_id": run_id,
        "directory": str(result_dir),
        "metadata_path": str(metadata_path),
        "metadata_changed": metadata_changed,
        "artifacts": synced,
    }

def execute(args: argparse.Namespace, client: httpx.Client) -> dict[str, Any]:
    if args.command == "create-project":
        response = client.post(
            "/mlflow/projects",
            json={"name": args.name, "description": args.description},
        )
    elif args.command == "create-model":
        response = client.post(
            f"/mlflow/projects/{args.project_id}/models",
            json={"name": args.name, "description": args.description},
        )
    elif args.command == "create-experiment":
        payload = {
            "project_id": args.project_id,
            "model_id": args.model_id,
            "name": args.name,
            "description": args.description,
            "tags": args.tags_json,
        }
        response = client.post("/mlflow/experiments", json=payload)
    elif args.command == "create-run":
        environment = {**detected_environment(), **args.environment_json}
        response = client.post(
            f"/mlflow/experiments/{args.experiment_id}/runs",
            json={"hyperparameters": args.hyperparameters_json, "environment": environment},
        )
    elif args.command == "start-run":
        current = client.get(f"/mlflow/runs/{args.run_id}")
        current.raise_for_status()
        environment = {
            **current.json().get("environment", {}),
            **detected_environment(),
            **(args.environment_json or {}),
        }
        response = client.post(
            f"/mlflow/runs/{args.run_id}/start",
            json={
                "environment": environment,
                "code_artifact_id": args.code_artifact_id,
                "weights_artifact_id": args.weights_artifact_id,
            },
        )
    elif args.command == "upload-artifact":
        return {"id": upload(client, args.experiment_id, args.kind, args.file), "kind": args.kind}
    elif args.command == "metric":
        response = client.post(
            f"/mlflow/runs/{args.run_id}/metrics",
            json={"name": args.name, "value": args.value, "step": args.step},
        )
    elif args.command == "finish-run":
        if args.status == "FAILED" and args.weights_file is not None:
            raise ValueError("--weights-file is only valid for a successful run")
        payload: dict[str, Any] = {"status": args.status, "error_summary": args.error_summary}
        for argument, kind, key in (
            (args.code_file, "CODE", "code_artifact_id"),
            (args.weights_file, "WEIGHTS", "weights_artifact_id"),
            (args.logs_file, "LOG", "logs_artifact_id"),
        ):
            if argument is not None:
                payload[key] = upload(client, args.experiment_id, kind, argument)
        response = client.post(f"/mlflow/runs/{args.run_id}/finish", json=payload)
    elif args.command == "list-snapshots":
        response = client.get(f"/mlflow/experiments/{args.experiment_id}/snapshots")
    elif args.command == "snapshot":
        response = client.get(f"/mlflow/snapshots/{args.snapshot_id}")
    elif args.command == "run-status":
        response = client.get(f"/mlflow/runs/{args.run_id}")
    elif args.command == "rebuild-status":
        response = client.get(f"/mlflow/rebuilds/{args.rebuild_id}")
    elif args.command == "sync-results":
        return sync_results(client, args.run_id)
    elif args.command == "download-artifact":
        return download_artifact_to_path(client, args.artifact_id, args.output)
    elif args.command == "rebuild":
        response = client.post(
            f"/mlflow/snapshots/{args.snapshot_id}/rebuilds",
            json={"environment": args.environment_json},
        )
    elif args.command == "finish-rebuild":
        if args.state == "REBUILD_FAILED" and not args.failure_summary:
            raise ValueError("--failure-summary is required for REBUILD_FAILED")
        if args.state != "REBUILD_SUCCESS" and args.weights_file is not None:
            raise ValueError("--weights-file is only valid for REBUILD_SUCCESS")
        attempt_response = client.get(f"/mlflow/rebuilds/{args.rebuild_id}")
        attempt_response.raise_for_status()
        snapshot_id = attempt_response.json()["snapshot_id"]
        snapshot_response = client.get(f"/mlflow/snapshots/{snapshot_id}")
        snapshot_response.raise_for_status()
        experiment_id = snapshot_response.json()["experiment_id"]
        payload = {
            "state": args.state,
            "failure_summary": args.failure_summary,
        }
        if args.logs_file is not None:
            payload["logs_artifact_id"] = upload(client, experiment_id, "LOG", args.logs_file)
        if args.weights_file is not None:
            payload["weights_artifact_id"] = upload(client, experiment_id, "WEIGHTS", args.weights_file)
        response = client.patch(f"/mlflow/rebuilds/{args.rebuild_id}", json=payload)
    else:
        raise ValueError("Unknown command")
    response.raise_for_status()
    return response.json()


def main() -> int:
    args = parser().parse_args()
    if not args.token:
        print("Set PRAYOG_TOKEN to an existing Prayog bearer token.", file=sys.stderr)
        return 2
    try:
        with httpx.Client(
            base_url=args.api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {args.token}"},
            timeout=60.0,
        ) as client:
            result = execute(args, client)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (httpx.HTTPError, ValueError, OSError) as exc:
        print(f"ML Flow request failed: {exc}", file=sys.stderr)
        if isinstance(exc, httpx.HTTPStatusError):
            print(exc.response.text, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
