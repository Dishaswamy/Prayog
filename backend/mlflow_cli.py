import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
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

    project_snapshot = commands.add_parser("snapshot-project")
    project_snapshot.add_argument("--experiment-id", type=int, required=True)
    project_snapshot.add_argument("--project-dir", type=Path, default=Path("."))
    project_snapshot.add_argument("--chunk-size-mib", type=int, default=8)

    materialize_snapshot = commands.add_parser("materialize-project-snapshot")
    materialize_snapshot.add_argument("--experiment-id", type=int, required=True)
    materialize_snapshot.add_argument("--manifest-artifact-id", required=True)
    materialize_snapshot.add_argument("--output-dir", type=Path, required=True)
    materialize_snapshot.add_argument("--data-dir", type=Path)

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
    rebuild.add_argument("--no-wait", dest="wait", action="store_false", default=True)
    rebuild.add_argument("--timeout-seconds", type=int, default=1800)
    rebuild.add_argument("--poll-interval", type=float, default=2.0)

    rebuild_run = commands.add_parser("run", help="Run a recorded snapshot with --rebuild")
    rebuild_run.add_argument("--rebuild", dest="snapshot_id", required=True)
    rebuild_run.add_argument("--environment-json", type=json_object, default={})
    rebuild_run.add_argument("--no-wait", dest="wait", action="store_false", default=True)
    rebuild_run.add_argument("--timeout-seconds", type=int, default=1800)
    rebuild_run.add_argument("--poll-interval", type=float, default=2.0)

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
        for key, expected_kind in (("weights", "WEIGHTS"), ("logs", "LOG")):
            artifact_id = artifact_ids[key]
            if not artifact_id:
                continue
            item = artifacts_by_id[artifact_id]
            if item.get("kind") != expected_kind:
                raise ValueError(f"Run {key} artifact metadata has an unexpected kind")
            digest = item.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest.lower()
            ):
                raise ValueError(f"Run {key} artifact metadata has an invalid SHA-256 digest")
            size = item.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_ARTIFACT_BYTES:
                raise ValueError(f"Run {key} artifact metadata has an invalid size")
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
        "artifacts": [
            {key: value for key, value in item.items() if key != "unchanged"}
            for item in synced
        ],
    }
    metadata_path = result_dir / "metadata.json"
    metadata_changed = _write_text_if_changed(
        metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )
    return {
        "run_id": run_id,
        "directory": str(result_dir),
        "metadata_path": str(metadata_path),
        "metadata_changed": metadata_changed,
        "artifacts": synced,
    }


EXCLUDED_PROJECT_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".nox", ".idea", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".terraform", "artifacts", "results", "dist", "build",
}
EXCLUDED_SECRET_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials", "credentials.json", "secrets.json", "service-account.json",
    "service_account.json", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    ".netrc", ".npmrc", ".pypirc", ".dockercfg", ".dockerconfigjson",
    ".git-credentials", ".envrc", "credentials.toml", "kubeconfig",
    "terraform.tfstate", "terraform.tfstate.backup",
}
LOCKFILE_NAMES = {
    "requirements.txt", "poetry.lock", "pipfile.lock", "uv.lock",
    "pdm.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "cargo.lock", "go.sum", "gemfile.lock", "composer.lock",
}
DATA_SUFFIXES = {
    ".csv", ".tsv", ".parquet", ".xlsx", ".xls", ".jsonl", ".feather",
    ".arrow", ".npy", ".npz", ".sqlite", ".db",
}
CONFIG_SUFFIXES = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}
MAX_PROJECT_SNAPSHOT_BYTES = 4 * 1024 * 1024 * 1024
MAX_PROJECT_FILES = 20000


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _project_file_role(relative_path: PurePosixPath) -> str:
    name = relative_path.name.lower()
    suffix = Path(name).suffix
    if name in LOCKFILE_NAMES:
        return "lockfile"
    if "seed" in name or suffix == ".seed":
        return "seed"
    if suffix in DATA_SUFFIXES:
        return "data"
    if suffix in CONFIG_SUFFIXES or name in {"config.json", "pyproject.toml"}:
        return "config"
    return "source"


def _project_inventory(project_root: Path) -> list[dict[str, Any]]:
    inventory = []
    total_size = 0
    for current, directories, filenames in os.walk(project_root, topdown=True, followlinks=False):
        current_path = Path(current)
        retained = []
        for name in sorted(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                raise ValueError(f"Project snapshots do not follow symbolic links: {candidate}")
            if name.lower() not in EXCLUDED_PROJECT_DIRS:
                retained.append(name)
        directories[:] = retained
        for name in sorted(filenames):
            path = current_path / name
            relative = path.relative_to(project_root)
            if any(part.lower() in EXCLUDED_PROJECT_DIRS for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Project snapshots do not follow symbolic links: {path}")
            lowered_name = name.lower()
            if (
                lowered_name in EXCLUDED_SECRET_NAMES
                or lowered_name.startswith(("credentials.", "secrets."))
                or lowered_name.endswith((".tfstate", ".tfstate.backup"))
            ):
                continue
            if lowered_name.startswith(".env.") and lowered_name != ".env.example":
                continue
            if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".secret"}:
                continue
            if not path.is_file():
                continue
            relative_posix = relative.as_posix()
            _safe_relative_path(relative_posix)
            if len(relative_posix) > 1024:
                raise ValueError(f"Project path is too long to snapshot: {relative_posix}")
            if relative_posix == "snapshot-manifest.json":
                raise ValueError("Project already contains reserved path snapshot-manifest.json")
            sha256, size_bytes = _hash_file(path)
            role = _project_file_role(PurePosixPath(relative.as_posix()))
            inventory.append({
                "path": relative.as_posix(),
                "size_bytes": size_bytes,
                "sha256": sha256,
                "role": role,
                "included_in_archive": role != "data",
            })
            total_size += size_bytes
            if total_size > MAX_PROJECT_SNAPSHOT_BYTES:
                raise ValueError("Project files exceed the 4 GiB snapshot limit")
            if len(inventory) > MAX_PROJECT_FILES:
                raise ValueError("Project contains too many files for a snapshot")
    inventory.sort(key=lambda item: item["path"])
    return inventory


def _write_project_archive(project_root: Path, archive_path: Path, files: list[dict[str, Any]]) -> None:
    internal_manifest = {
        "format_version": 1,
        "files": files,
    }
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in files:
            if not item["included_in_archive"]:
                continue
            relative = PurePosixPath(item["path"])
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            file_digest = hashlib.sha256()
            file_size = 0
            with archive.open(info, "w") as target, (project_root / Path(*relative.parts)).open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    file_digest.update(chunk)
                    file_size += len(chunk)
                    target.write(chunk)
            if file_size != item["size_bytes"] or file_digest.hexdigest() != item["sha256"]:
                raise ValueError(f"Project file changed while snapshotting: {item['path']}")
        manifest_info = zipfile.ZipInfo("snapshot-manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o600 << 16
        manifest_bytes = json.dumps(internal_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        archive.writestr(manifest_info, manifest_bytes)


def _known_artifacts(client: httpx.Client, experiment_id: int) -> dict[tuple[str, int, str], dict[str, Any]]:
    response = client.get(f"/mlflow/experiments/{experiment_id}/artifacts")
    response.raise_for_status()
    known = {}
    for item in response.json():
        known.setdefault((item["sha256"], item["size_bytes"], item["kind"]), item)
    return known


def _upload_if_missing(
    client: httpx.Client,
    experiment_id: int,
    path: Path,
    kind: str,
    known: dict[tuple[str, int, str], dict[str, Any]],
) -> tuple[str, bool, str, int]:
    sha256, size_bytes = _hash_file(path)
    key = (sha256, size_bytes, kind)
    existing = known.get(key)
    if existing is not None:
        return existing["id"], False, sha256, size_bytes
    artifact_id = upload(client, experiment_id, kind, path)
    known[key] = {"id": artifact_id, "sha256": sha256, "size_bytes": size_bytes, "kind": kind}
    return artifact_id, True, sha256, size_bytes


def snapshot_project(
    client: httpx.Client,
    experiment_id: int,
    project_dir: Path,
    chunk_size_mib: int = 8,
) -> dict[str, Any]:
    if experiment_id < 1:
        raise ValueError("Experiment ID must be a positive integer")
    if chunk_size_mib < 1 or chunk_size_mib > 32:
        raise ValueError("Chunk size must be between 1 and 32 MiB")
    if project_dir.is_symlink() or not project_dir.is_dir():
        raise ValueError("Project directory must be an existing, non-symbolic-link directory")
    project_root = project_dir.resolve()
    files = _project_inventory(project_root)
    if not files:
        raise ValueError("Project snapshot contains no eligible files")

    with tempfile.TemporaryDirectory(prefix="prayog-project-snapshot-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        archive_path = temporary_root / "project-snapshot.zip"
        _write_project_archive(project_root, archive_path, files)
        archive_sha256, archive_size = _hash_file(archive_path)
        if archive_size > MAX_PROJECT_SNAPSHOT_BYTES:
            raise ValueError("Project snapshot exceeds the 4 GiB materialization limit")
        chunk_size_bytes = chunk_size_mib * 1024 * 1024
        known = _known_artifacts(client, experiment_id)
        chunks = []
        uploaded_chunks = 0
        reused_chunks = 0
        with archive_path.open("rb") as archive:
            index = 0
            while True:
                payload = archive.read(chunk_size_bytes)
                if not payload:
                    break
                chunk_path = temporary_root / f"chunk-{index:06d}.bin"
                chunk_path.write_bytes(payload)
                artifact_id, uploaded_new, sha256, size_bytes = _upload_if_missing(
                    client, experiment_id, chunk_path, "CODE", known
                )
                chunks.append({
                    "index": index,
                    "artifact_id": artifact_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                })
                if uploaded_new:
                    uploaded_chunks += 1
                else:
                    reused_chunks += 1
                index += 1

        if _project_inventory(project_root) != files:
            raise ValueError("Project changed while snapshotting; retry the command")
        manifest = {
            "format_version": 1,
            "experiment_id": experiment_id,
            "archive_sha256": archive_sha256,
            "archive_size_bytes": archive_size,
            "chunk_size_bytes": chunk_size_bytes,
            "files": files,
            "chunks": chunks,
        }
        manifest_path = temporary_root / "project-snapshot-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_id, manifest_uploaded, manifest_sha256, manifest_size = _upload_if_missing(
            client, experiment_id, manifest_path, "CODE", known
        )
        return {
            "experiment_id": experiment_id,
            "manifest_artifact_id": manifest_id,
            "manifest_sha256": manifest_sha256,
            "manifest_size_bytes": manifest_size,
            "archive_sha256": archive_sha256,
            "archive_size_bytes": archive_size,
            "file_count": len(files),
            "chunk_count": len(chunks),
            "uploaded_chunks": uploaded_chunks,
            "reused_chunks": reused_chunks,
            "manifest_uploaded": manifest_uploaded,
        }


def _safe_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Snapshot contains an invalid file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Snapshot contains an unsafe file path")
    if path.parts and ":" in path.parts[0]:
        raise ValueError("Snapshot contains a drive-qualified file path")
    return path


def _validate_snapshot_files(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_PROJECT_FILES:
        raise ValueError("Snapshot file inventory is invalid")
    result = []
    seen = set()
    total_size = 0
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Snapshot file inventory is invalid")
        relative = _safe_relative_path(item.get("path"))
        size_bytes = item.get("size_bytes")
        sha256 = item.get("sha256")
        role = item.get("role")
        included_in_archive = item.get("included_in_archive")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            raise ValueError("Snapshot file size is invalid")
        if not isinstance(sha256, str) or len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ValueError("Snapshot file digest is invalid")
        if not isinstance(role, str) or role not in {"source", "lockfile", "seed", "config", "data"}:
            raise ValueError("Snapshot file role is invalid")
        if not isinstance(included_in_archive, bool) or included_in_archive == (role == "data"):
            raise ValueError("Snapshot file inclusion policy is invalid")
        path_string = relative.as_posix()
        if len(path_string) > 1024:
            raise ValueError("Snapshot file path is too long")
        if path_string == "snapshot-manifest.json" or path_string in seen:
            raise ValueError("Snapshot contains a duplicate or reserved path")
        seen.add(path_string)
        total_size += size_bytes
        if total_size > MAX_PROJECT_SNAPSHOT_BYTES:
            raise ValueError("Snapshot file inventory exceeds the 4 GiB limit")
        result.append({
            "path": path_string,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "role": role,
            "included_in_archive": included_in_archive,
        })
    if result != sorted(result, key=lambda item: item["path"]):
        raise ValueError("Snapshot file inventory is not sorted")
    return result


def _extract_verified_project_archive(
    archive_path: Path,
    staging_directory: Path,
    expected_files: list[dict[str, Any]],
) -> None:
    expected = {
        item["path"]: item for item in expected_files if item["included_in_archive"]
    }
    extracted = set()
    internal_manifest = None
    total_size = 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > MAX_PROJECT_FILES + 1:
            raise ValueError("Snapshot archive contains too many entries")
        names = set()
        for info in infos:
            relative = _safe_relative_path(info.filename.rstrip("/"))
            name = relative.as_posix()
            if name in names:
                raise ValueError("Snapshot archive contains duplicate paths")
            names.add(name)
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("Snapshot archive contains a symbolic link or special file")
            if info.is_dir() and file_type not in {0, stat.S_IFDIR}:
                raise ValueError("Snapshot directory entry has an invalid type")
            if not info.is_dir() and file_type == stat.S_IFDIR:
                raise ValueError("Snapshot file entry has an invalid type")
            destination = staging_directory.joinpath(*relative.parts)
            if info.is_dir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if name == "snapshot-manifest.json":
                if info.file_size > 32 * 1024 * 1024:
                    raise ValueError("Snapshot's internal manifest is too large")
                with archive.open(info, "r") as source:
                    raw = source.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("Snapshot's internal manifest is too large")
                internal_manifest = json.loads(raw.decode("utf-8"))
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.write_bytes(raw)
                os.chmod(destination, 0o600)
                continue
            expected_item = expected.get(name)
            if expected_item is None:
                raise ValueError("Snapshot archive contains a file absent from its manifest")
            if info.file_size != expected_item["size_bytes"]:
                raise ValueError("Snapshot file size does not match its manifest")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest = hashlib.sha256()
            file_size = 0
            with archive.open(info, "r") as source, destination.open("xb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    file_size += len(chunk)
                    total_size += len(chunk)
                    if total_size > MAX_PROJECT_SNAPSHOT_BYTES:
                        raise ValueError("Snapshot extraction exceeds the 4 GiB limit")
                    digest.update(chunk)
                    target.write(chunk)
            if file_size != expected_item["size_bytes"] or digest.hexdigest() != expected_item["sha256"]:
                raise ValueError(f"Snapshot file integrity verification failed: {name}")
            os.chmod(destination, 0o600)
            extracted.add(name)
    if extracted != set(expected):
        raise ValueError("Snapshot archive is missing files listed in its manifest")
    if (
        not isinstance(internal_manifest, dict)
        or type(internal_manifest.get("format_version")) is not int
        or internal_manifest.get("format_version") != 1
    ):
        raise ValueError("Snapshot's internal manifest is invalid")
    if internal_manifest.get("files") != expected_files:
        raise ValueError("Snapshot's internal and external manifests do not match")


def materialize_project_snapshot(
    client: httpx.Client,
    experiment_id: int,
    manifest_artifact_id: str,
    output_dir: Path,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    if experiment_id < 1:
        raise ValueError("Experiment ID must be a positive integer")
    if not manifest_artifact_id:
        raise ValueError("Manifest artifact ID is required")
    target = output_dir
    if target.exists() or target.is_symlink():
        raise ValueError("Output directory already exists; choose a new destination")
    target.parent.mkdir(parents=True, exist_ok=True)
    artifacts_response = client.get(f"/mlflow/experiments/{experiment_id}/artifacts")
    artifacts_response.raise_for_status()
    artifacts = {item["id"]: item for item in artifacts_response.json()}
    manifest_artifact = artifacts.get(manifest_artifact_id)
    if (
        manifest_artifact is None
        or manifest_artifact.get("kind") != "CODE"
        or manifest_artifact.get("size_bytes", 0) > 32 * 1024 * 1024
    ):
        raise ValueError("Project snapshot manifest artifact was not found or is too large")
    with tempfile.TemporaryDirectory(prefix="prayog-project-materialize-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        manifest_path = temporary_root / "manifest.json"
        download_artifact_to_path(
            client, manifest_artifact_id, manifest_path,
            expected_sha256=manifest_artifact["sha256"],
            expected_size=manifest_artifact["size_bytes"],
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("format_version")) is not int
            or manifest.get("format_version") != 1
        ):
            raise ValueError("Project snapshot manifest format is unsupported")
        if (
            type(manifest.get("experiment_id")) is not int
            or manifest.get("experiment_id") != experiment_id
        ):
            raise ValueError("Project snapshot manifest belongs to another experiment")
        expected_files = _validate_snapshot_files(manifest.get("files"))
        data_files = [item for item in expected_files if item["role"] == "data"]
        if data_files:
            if data_dir is None or data_dir.is_symlink() or not data_dir.is_dir():
                raise ValueError("Snapshot contains data fingerprints; supply an existing --data-dir")
            data_root = data_dir.resolve()
            for item in data_files:
                candidate = data_root
                for part in _safe_relative_path(item["path"]).parts:
                    candidate = candidate / part
                    if candidate.is_symlink():
                        raise ValueError("Dataset fingerprint path contains a symbolic link")
                if not candidate.is_file():
                    raise ValueError(f"Dataset fingerprint file is missing: {item['path']}")
                data_sha256, data_size = _hash_file(candidate)
                if data_sha256 != item["sha256"] or data_size != item["size_bytes"]:
                    raise ValueError(f"Dataset fingerprint verification failed: {item['path']}")
        chunks = manifest.get("chunks")
        if not isinstance(chunks, list) or not chunks or len(chunks) > 4096:
            raise ValueError("Project snapshot chunk manifest is invalid")
        chunk_size_expected = manifest.get("chunk_size_bytes")
        if (
            not isinstance(chunk_size_expected, int)
            or isinstance(chunk_size_expected, bool)
            or not 1 <= chunk_size_expected <= 32 * 1024 * 1024
        ):
            raise ValueError("Project snapshot chunk size is invalid")
        archive_size_expected = manifest.get("archive_size_bytes")
        archive_sha_expected = manifest.get("archive_sha256")
        if (
            not isinstance(archive_size_expected, int)
            or isinstance(archive_size_expected, bool)
            or archive_size_expected < 1
            or archive_size_expected > MAX_PROJECT_SNAPSHOT_BYTES
        ):
            raise ValueError("Project snapshot archive size is invalid")
        if not isinstance(archive_sha_expected, str) or len(archive_sha_expected) != 64 or any(ch not in "0123456789abcdef" for ch in archive_sha_expected):
            raise ValueError("Project snapshot archive digest is invalid")
        declared_chunk_size = 0
        for chunk in chunks:
            if (
                not isinstance(chunk, dict)
                or not isinstance(chunk.get("size_bytes"), int)
                or isinstance(chunk.get("size_bytes"), bool)
                or chunk["size_bytes"] < 1
                or chunk["size_bytes"] > chunk_size_expected
            ):
                raise ValueError("Project snapshot chunk manifest is invalid")
            declared_chunk_size += chunk["size_bytes"]
        if declared_chunk_size != archive_size_expected:
            raise ValueError("Project snapshot chunk sizes do not match archive metadata")
        archive_path = temporary_root / "project-snapshot.zip"
        archive_digest = hashlib.sha256()
        archive_size = 0
        with archive_path.open("xb") as combined:
            for expected_index, chunk in enumerate(chunks):
                if (
                    not isinstance(chunk, dict)
                    or type(chunk.get("index")) is not int
                    or chunk.get("index") != expected_index
                ):
                    raise ValueError("Project snapshot chunks are not in canonical order")
                artifact_id = chunk.get("artifact_id")
                sha256 = chunk.get("sha256")
                size_bytes = chunk.get("size_bytes")
                if (
                    not isinstance(artifact_id, str)
                    or not isinstance(sha256, str)
                    or len(sha256) != 64
                    or any(ch not in "0123456789abcdef" for ch in sha256)
                    or not isinstance(size_bytes, int)
                    or isinstance(size_bytes, bool)
                ):
                    raise ValueError("Project snapshot chunk metadata is invalid")
                artifact = artifacts.get(artifact_id)
                if (
                    artifact is None or artifact.get("kind") != "CODE"
                    or artifact.get("sha256") != sha256 or artifact.get("size_bytes") != size_bytes
                    or not isinstance(sha256, str) or len(sha256) != 64
                    or not isinstance(size_bytes, int) or size_bytes < 1
                    or size_bytes > chunk_size_expected
                    or (expected_index < len(chunks) - 1 and size_bytes != chunk_size_expected)
                ):
                    raise ValueError("Project snapshot chunk metadata is invalid")
                chunk_path = temporary_root / f"chunk-{expected_index:06d}.bin"
                download_artifact_to_path(
                    client, artifact_id, chunk_path,
                    expected_sha256=sha256, expected_size=size_bytes,
                )
                with chunk_path.open("rb") as source:
                    for data in iter(lambda: source.read(1024 * 1024), b""):
                        archive_size += len(data)
                        if archive_size > MAX_PROJECT_SNAPSHOT_BYTES:
                            raise ValueError("Project snapshot archive exceeds the 4 GiB limit")
                        archive_digest.update(data)
                        combined.write(data)
        if archive_size != archive_size_expected or archive_digest.hexdigest() != archive_sha_expected:
            raise ValueError("Project snapshot archive checksum verification failed")
        staging_directory = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            _extract_verified_project_archive(archive_path, staging_directory, expected_files)
            os.replace(staging_directory, target)
            staging_directory = None
        finally:
            if staging_directory is not None:
                shutil.rmtree(staging_directory, ignore_errors=True)
    return {
        "experiment_id": experiment_id,
        "manifest_artifact_id": manifest_artifact_id,
        "output_dir": str(target),
        "file_count": len(expected_files),
        "materialized_file_count": sum(1 for item in expected_files if item["included_in_archive"]),
        "data_fingerprints": [
            {"path": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in expected_files if item["role"] == "data"
        ],
        "data_fingerprints_verified": True,
        "archive_sha256": archive_sha_expected,
        "verified": True,
    }

def execute(args: argparse.Namespace, client: httpx.Client) -> dict[str, Any]:
    if args.command == "create-model":
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
    elif args.command == "snapshot-project":
        return snapshot_project(client, args.experiment_id, args.project_dir, args.chunk_size_mib)
    elif args.command == "materialize-project-snapshot":
        return materialize_project_snapshot(
            client, args.experiment_id, args.manifest_artifact_id, args.output_dir, args.data_dir
        )
    elif args.command == "run-status":
        response = client.get(f"/mlflow/runs/{args.run_id}")
    elif args.command == "rebuild-status":
        response = client.get(f"/mlflow/rebuilds/{args.rebuild_id}")
    elif args.command == "sync-results":
        return sync_results(client, args.run_id)
    elif args.command == "download-artifact":
        return download_artifact_to_path(client, args.artifact_id, args.output)
    elif args.command in {"rebuild", "run"}:
        if args.timeout_seconds < 1:
            raise ValueError("--timeout-seconds must be positive")
        if args.poll_interval <= 0:
            raise ValueError("--poll-interval must be positive")
        response = client.post(
            f"/mlflow/snapshots/{args.snapshot_id}/rebuilds",
            json={"environment": args.environment_json},
        )
        response.raise_for_status()
        attempt = response.json()
        if not args.wait or attempt.get("state") in {"REBUILD_SUCCESS", "REBUILD_FAILED", "REBUILD_CANCELLED"}:
            return attempt
        rebuild_id = attempt.get("id")
        if rebuild_id is None:
            raise ValueError("Rebuild response did not include an Attempt ID")
        deadline = time.monotonic() + args.timeout_seconds
        while True:
            status_response = client.get(f"/mlflow/rebuilds/{rebuild_id}")
            status_response.raise_for_status()
            attempt = status_response.json()
            if attempt.get("state") in {"REBUILD_SUCCESS", "REBUILD_FAILED", "REBUILD_CANCELLED"}:
                return attempt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for rebuild {rebuild_id}; check it with rebuild-status --rebuild-id {rebuild_id}")
            time.sleep(min(args.poll_interval, remaining))
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
        if result.get("state") == "REBUILD_FAILED":
            return 1
        return 0
    except (httpx.HTTPError, ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"ML Flow request failed: {exc}", file=sys.stderr)
        if isinstance(exc, httpx.HTTPStatusError):
            print(exc.response.text, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
