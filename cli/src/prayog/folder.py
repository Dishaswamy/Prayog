"""Scanning a project folder and uploading it to the server (`prayog project link` / `update`).

The folder stays on the user's disk; the server keeps its own copy so a run or rebuild never
depends on files that may have changed. What is stored for one version of a folder:

  * the source, configs, lockfiles and seeds, as one zip archive cut into chunks. Each chunk is an
    ordinary CODE artifact, so a chunk the server already holds is not sent again (the server
    addresses stored bytes by BLAKE3, so identical content is kept once);
  * a fingerprint (path, size, SHA-256) for each dataset file. Datasets are not uploaded: a later
    restore checks the user's local copy still matches;
  * a manifest listing all of the above, uploaded last. Its artifact id names the version.

Secrets (.env, keys, credentials, state files) and tool/VCS directories are never read.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from prayog.client import Client
from prayog.errors import CliError, USAGE

FORMAT_VERSION = 1
MANIFEST_ENTRY = "snapshot-manifest.json"
LINK_DIR = ".prayog"
MAX_FOLDER_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILES = 20000
MAX_ARTIFACT_BYTES = 250 * 1024 * 1024
DEFAULT_CHUNK_MIB = 8
READ_BLOCK = 1024 * 1024

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".nox", ".idea", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".terraform",
    "artifacts", "results", "dist", "build", LINK_DIR,
}
EXCLUDED_SECRET_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials", "credentials.json", "secrets.json", "service-account.json",
    "service_account.json", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    ".netrc", ".npmrc", ".pypirc", ".dockercfg", ".dockerconfigjson",
    ".git-credentials", ".envrc", "credentials.toml", "kubeconfig",
    "terraform.tfstate", "terraform.tfstate.backup",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".secret"}
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


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(READ_BLOCK), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def safe_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Snapshot contains an invalid file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Snapshot contains an unsafe file path")
    if path.parts and ":" in path.parts[0]:
        raise ValueError("Snapshot contains a drive-qualified file path")
    return path


def file_role(relative: PurePosixPath) -> str:
    name = relative.name.lower()
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


def _is_secret(name: str, path: Path) -> bool:
    lowered = name.lower()
    return (
        lowered in EXCLUDED_SECRET_NAMES
        or lowered.startswith(("credentials.", "secrets."))
        or lowered.endswith((".tfstate", ".tfstate.backup"))
        or (lowered.startswith(".env.") and lowered != ".env.example")
        or path.suffix.lower() in SECRET_SUFFIXES
    )


def inventory(root: Path) -> list[dict[str, Any]]:
    """Every eligible file under root, sorted by path, each with size, SHA-256 and role."""
    files: list[dict[str, Any]] = []
    total = 0
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        here = Path(current)
        kept = []
        for name in sorted(directories):
            if (here / name).is_symlink():
                raise ValueError(f"Folders with symbolic links cannot be uploaded: {here / name}")
            if name.lower() not in EXCLUDED_DIRS:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            path = here / name
            if path.is_symlink():
                raise ValueError(f"Folders with symbolic links cannot be uploaded: {path}")
            if _is_secret(name, path) or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            safe_relative_path(relative)
            if len(relative) > 1024:
                raise ValueError(f"Path is too long to upload: {relative}")
            if relative == MANIFEST_ENTRY:
                raise ValueError(f"The folder already contains the reserved file {MANIFEST_ENTRY}")
            sha256, size = hash_file(path)
            role = file_role(PurePosixPath(relative))
            files.append({"path": relative, "size_bytes": size, "sha256": sha256,
                          "role": role, "included_in_archive": role != "data"})
            total += size
            if total > MAX_FOLDER_BYTES:
                raise ValueError("The folder is over the 4 GiB limit")
            if len(files) > MAX_FILES:
                raise ValueError(f"The folder has more than {MAX_FILES} files")
    files.sort(key=lambda item: item["path"])
    return files


def inventory_fingerprint(files: list[dict[str, Any]]) -> str:
    """One digest of the whole inventory, to tell whether anything changed since the last upload."""
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_archive(root: Path, archive_path: Path, files: list[dict[str, Any]]) -> None:
    """A deterministic zip of the non-data files, with the inventory inside as a second copy."""
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in files:
            if not item["included_in_archive"]:
                continue
            relative = PurePosixPath(item["path"])
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            digest = hashlib.sha256()
            size = 0
            with archive.open(info, "w") as target, (root / Path(*relative.parts)).open("rb") as source:
                for block in iter(lambda: source.read(READ_BLOCK), b""):
                    digest.update(block)
                    size += len(block)
                    target.write(block)
            if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                raise ValueError(f"A file changed while it was being uploaded: {item['path']}")
        info = zipfile.ZipInfo(MANIFEST_ENTRY, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        body = json.dumps({"format_version": FORMAT_VERSION, "files": files},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        archive.writestr(info, body)


def _known_artifacts(client: Client, experiment_id: int) -> dict:
    known: dict = {}
    for item in client.get(f"/mlflow/experiments/{experiment_id}/artifacts"):
        known.setdefault((item["sha256"], item["size_bytes"], item["kind"]), item)
    return known


def _upload_if_missing(client: Client, experiment_id: int, payload: bytes, known: dict) -> tuple:
    """(artifact id, was_uploaded, sha256). Bytes the experiment already holds are not sent."""
    if not payload or len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"An upload must be between 1 byte and {MAX_ARTIFACT_BYTES} bytes")
    sha256 = hashlib.sha256(payload).hexdigest()
    existing = known.get((sha256, len(payload), "CODE"))
    if existing is not None:
        return existing["id"], False, sha256
    created = client.upload(f"/mlflow/experiments/{experiment_id}/artifacts", {"kind": "CODE"}, payload)
    known[(sha256, len(payload), "CODE")] = {"id": created["id"]}
    return created["id"], True, sha256


def check_folder(folder: Path) -> Path:
    if folder.is_symlink() or not folder.is_dir():
        raise CliError(f"'{folder}' is not a folder (symbolic links are not followed).", USAGE)
    return folder.resolve()


def upload_folder(client: Client, experiment_id: int, root: Path, files: list[dict[str, Any]],
                  chunk_mib: int = DEFAULT_CHUNK_MIB) -> dict[str, Any]:
    """Upload one version of the folder and return its manifest artifact id and counts."""
    if not 1 <= chunk_mib <= 32:
        raise ValueError("Chunk size must be between 1 and 32 MiB")
    if not files:
        raise ValueError("The folder has no files that can be uploaded")
    with tempfile.TemporaryDirectory(prefix="prayog-upload-") as scratch:
        archive_path = Path(scratch) / "project.zip"
        write_archive(root, archive_path, files)
        archive_sha256, archive_size = hash_file(archive_path)
        if archive_size > MAX_FOLDER_BYTES:
            raise ValueError("The folder is over the 4 GiB limit once archived")
        known = _known_artifacts(client, experiment_id)
        chunk_bytes = chunk_mib * 1024 * 1024
        chunks = []
        uploaded = reused = 0
        with archive_path.open("rb") as archive:
            while True:
                payload = archive.read(chunk_bytes)
                if not payload:
                    break
                artifact_id, sent, sha256 = _upload_if_missing(client, experiment_id, payload, known)
                chunks.append({"index": len(chunks), "artifact_id": artifact_id,
                               "sha256": sha256, "size_bytes": len(payload)})
                if sent:
                    uploaded += 1
                else:
                    reused += 1
    if inventory(root) != files:
        raise ValueError("The folder changed while it was being uploaded; run the command again")
    manifest = {
        "format_version": FORMAT_VERSION,
        "experiment_id": experiment_id,
        "archive_sha256": archive_sha256,
        "archive_size_bytes": archive_size,
        "chunk_size_bytes": chunk_bytes,
        "files": files,
        "chunks": chunks,
    }
    body = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_id, manifest_sent, manifest_sha256 = _upload_if_missing(client, experiment_id, body, known)
    return {
        "manifest_artifact_id": manifest_id,
        "manifest_sha256": manifest_sha256,
        "archive_sha256": archive_sha256,
        "file_count": len(files),
        "data_fingerprints": sum(1 for item in files if item["role"] == "data"),
        "uploaded_chunks": uploaded,
        "reused_chunks": reused,
        "manifest_uploaded": manifest_sent,
    }


# The link file: where this folder was last uploaded to.

def link_path(folder: Path) -> Path:
    return folder / LINK_DIR / "link.json"


def read_link(folder: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(link_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and {"project_id", "experiment_id", "versions"} <= data.keys() else None


def write_link(folder: Path, link: dict[str, Any]) -> None:
    path = link_path(folder)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps(link, indent=2, sort_keys=True) + "\n", encoding="utf-8")
