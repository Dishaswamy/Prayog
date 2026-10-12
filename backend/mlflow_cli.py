import argparse
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
    elif args.command == "download-artifact":
        if not args.output.parent.exists():
            raise ValueError(f"Output directory does not exist: {args.output.parent}")
        temporary_path = None
        total = 0
        try:
            with client.stream("GET", f"/mlflow/artifacts/{args.artifact_id}/content") as response:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=f".{args.output.name}.", suffix=".part",
                    dir=args.output.parent, delete=False,
                ) as output:
                    temporary_path = Path(output.name)
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_ARTIFACT_BYTES:
                            raise ValueError("Downloaded artifact exceeds the 250 MiB limit")
                        output.write(chunk)
            os.replace(temporary_path, args.output)
            os.chmod(args.output, 0o600)
            return {"path": str(args.output), "size_bytes": total}
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
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
