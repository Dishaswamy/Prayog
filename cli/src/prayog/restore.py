"""Restoring a stored version of a folder (`prayog project pull`).

Everything fetched is checked against the manifest before it is trusted: each chunk's size and
SHA-256, the reassembled archive, every extracted file, and (for datasets, which are not stored)
the user's local copy. A restore that fails any check leaves nothing behind.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from prayog.client import Client
from prayog.folder import (
    FORMAT_VERSION, MANIFEST_ENTRY, MAX_ARTIFACT_BYTES, MAX_FILES, MAX_FOLDER_BYTES, READ_BLOCK,
    hash_file, safe_relative_path,
)

MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_CHUNKS = 4096
ROLES = {"source", "lockfile", "seed", "config", "data"}


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_files(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise ValueError("Snapshot file inventory is invalid")
    result, seen, total = [], set(), 0
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Snapshot file inventory is invalid")
        relative = safe_relative_path(item.get("path"))
        size, sha256, role, included = (item.get("size_bytes"), item.get("sha256"),
                                        item.get("role"), item.get("included_in_archive"))
        if not _is_int(size) or size < 0:
            raise ValueError("Snapshot file size is invalid")
        if not _is_digest(sha256):
            raise ValueError("Snapshot file digest is invalid")
        if role not in ROLES:
            raise ValueError("Snapshot file role is invalid")
        if not isinstance(included, bool) or included == (role == "data"):
            raise ValueError("Snapshot file inclusion policy is invalid")
        path = relative.as_posix()
        if len(path) > 1024 or path == MANIFEST_ENTRY or path in seen:
            raise ValueError("Snapshot contains a duplicate, reserved or over-long path")
        seen.add(path)
        total += size
        if total > MAX_FOLDER_BYTES:
            raise ValueError("Snapshot file inventory exceeds the 4 GiB limit")
        result.append({"path": path, "size_bytes": size, "sha256": sha256,
                       "role": role, "included_in_archive": included})
    if result != sorted(result, key=lambda item: item["path"]):
        raise ValueError("Snapshot file inventory is not sorted")
    return result


def extract_archive(archive_path: Path, staging: Path, expected_files: list[dict[str, Any]]) -> None:
    expected = {item["path"]: item for item in expected_files if item["included_in_archive"]}
    extracted, internal, total = set(), None, 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES + 1:
            raise ValueError("Snapshot archive contains too many entries")
        names = set()
        for info in infos:
            relative = safe_relative_path(info.filename.rstrip("/"))
            name = relative.as_posix()
            if name in names:
                raise ValueError("Snapshot archive contains duplicate paths")
            names.add(name)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("Snapshot archive contains a symbolic link or special file")
            if info.is_dir() and kind not in {0, stat.S_IFDIR}:
                raise ValueError("Snapshot directory entry has an invalid type")
            if not info.is_dir() and kind == stat.S_IFDIR:
                raise ValueError("Snapshot file entry has an invalid type")
            destination = staging.joinpath(*relative.parts)
            if info.is_dir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if name == MANIFEST_ENTRY:
                with archive.open(info, "r") as source:
                    raw = source.read(MAX_MANIFEST_BYTES + 1)
                if len(raw) > MAX_MANIFEST_BYTES:
                    raise ValueError("Snapshot's internal manifest is too large")
                internal = json.loads(raw.decode("utf-8"))
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.write_bytes(raw)
                os.chmod(destination, 0o600)
                continue
            item = expected.get(name)
            if item is None:
                raise ValueError("Snapshot archive contains a file absent from its manifest")
            if info.file_size != item["size_bytes"]:
                raise ValueError("Snapshot file size does not match its manifest")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with archive.open(info, "r") as source, destination.open("xb") as target:
                for block in iter(lambda: source.read(READ_BLOCK), b""):
                    size += len(block)
                    total += len(block)
                    if total > MAX_FOLDER_BYTES:
                        raise ValueError("Snapshot extraction exceeds the 4 GiB limit")
                    digest.update(block)
                    target.write(block)
            if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                raise ValueError(f"Snapshot file integrity verification failed: {name}")
            os.chmod(destination, 0o600)
            extracted.add(name)
    if extracted != set(expected):
        raise ValueError("Snapshot archive is missing files listed in its manifest")
    if not isinstance(internal, dict) or internal.get("format_version") != FORMAT_VERSION:
        raise ValueError("Snapshot's internal manifest is invalid")
    if internal.get("files") != expected_files:
        raise ValueError("Snapshot's internal and external manifests do not match")


def _check_datasets(expected_files: list[dict[str, Any]], data_dir: Optional[Path]) -> None:
    data_files = [item for item in expected_files if item["role"] == "data"]
    if not data_files:
        return
    if data_dir is None or data_dir.is_symlink() or not data_dir.is_dir():
        raise ValueError("This version has dataset fingerprints; give the folder holding your datasets (--data-dir)")
    root = data_dir.resolve()
    for item in data_files:
        candidate = root
        for part in safe_relative_path(item["path"]).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("Dataset path contains a symbolic link")
        if not candidate.is_file():
            raise ValueError(f"Dataset file is missing: {item['path']}")
        sha256, size = hash_file(candidate)
        if sha256 != item["sha256"] or size != item["size_bytes"]:
            raise ValueError(f"Dataset fingerprint verification failed: {item['path']}")


def _download(client: Client, artifact_id: str, destination: Path, sha256: str, size: int) -> None:
    digest, total = hashlib.sha256(), 0
    with client.stream(f"/mlflow/artifacts/{artifact_id}/content") as response, destination.open("xb") as target:
        for block in response.iter_bytes():
            total += len(block)
            if total > MAX_ARTIFACT_BYTES:
                raise ValueError("A downloaded piece exceeds the 250 MiB limit")
            digest.update(block)
            target.write(block)
    if total != size:
        raise ValueError("A downloaded piece's size does not match the server's record")
    if digest.hexdigest() != sha256:
        raise ValueError("A downloaded piece's checksum does not match the server's record")


def _validated_chunks(manifest: dict[str, Any], artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = manifest.get("chunks")
    chunk_limit = manifest.get("chunk_size_bytes")
    if not isinstance(chunks, list) or not chunks or len(chunks) > MAX_CHUNKS:
        raise ValueError("Snapshot chunk manifest is invalid")
    if not _is_int(chunk_limit) or not 1 <= chunk_limit <= 32 * 1024 * 1024:
        raise ValueError("Snapshot chunk size is invalid")
    declared = 0
    for position, chunk in enumerate(chunks):
        if not isinstance(chunk, dict) or chunk.get("index") != position or not _is_int(chunk.get("index")):
            raise ValueError("Snapshot chunks are not in canonical order")
        size, sha256 = chunk.get("size_bytes"), chunk.get("sha256")
        if not _is_int(size) or not 1 <= size <= chunk_limit or not _is_digest(sha256):
            raise ValueError("Snapshot chunk manifest is invalid")
        if position < len(chunks) - 1 and size != chunk_limit:
            raise ValueError("Snapshot chunk manifest is invalid")
        recorded = artifacts.get(chunk.get("artifact_id"))
        if (recorded is None or recorded.get("kind") != "CODE"
                or recorded.get("sha256") != sha256 or recorded.get("size_bytes") != size):
            raise ValueError("Snapshot chunk metadata is invalid")
        declared += size
    if declared != manifest.get("archive_size_bytes"):
        raise ValueError("Snapshot chunk sizes do not match archive metadata")
    return chunks


def pull_version(client: Client, experiment_id: int, manifest_artifact_id: str, target: Path,
                 data_dir: Optional[Path] = None) -> dict[str, Any]:
    """Rebuild a version's folder at `target` (which must not exist). Raises ValueError on any mismatch."""
    if target.exists() or target.is_symlink():
        raise ValueError(f"'{target}' already exists; choose a new destination")
    target.parent.mkdir(parents=True, exist_ok=True)
    artifacts = {item["id"]: item for item in client.get(f"/mlflow/experiments/{experiment_id}/artifacts")}
    recorded = artifacts.get(manifest_artifact_id)
    if recorded is None or recorded.get("kind") != "CODE" or recorded.get("size_bytes", 0) > MAX_MANIFEST_BYTES:
        raise ValueError("That version was not found in this project")
    with tempfile.TemporaryDirectory(prefix="prayog-pull-") as scratch:
        scratch_dir = Path(scratch)
        manifest_file = scratch_dir / "manifest.json"
        _download(client, manifest_artifact_id, manifest_file, recorded["sha256"], recorded["size_bytes"])
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError("This version's manifest format is not supported")
        if manifest.get("experiment_id") != experiment_id or not _is_int(manifest.get("experiment_id")):
            raise ValueError("This version belongs to another project")
        files = validate_files(manifest.get("files"))
        _check_datasets(files, data_dir)
        archive_size, archive_sha = manifest.get("archive_size_bytes"), manifest.get("archive_sha256")
        if not _is_int(archive_size) or not 1 <= archive_size <= MAX_FOLDER_BYTES or not _is_digest(archive_sha):
            raise ValueError("Snapshot archive metadata is invalid")
        chunks = _validated_chunks(manifest, artifacts)
        archive_path = scratch_dir / "project.zip"
        digest, total = hashlib.sha256(), 0
        with archive_path.open("xb") as combined:
            for chunk in chunks:
                piece = scratch_dir / f"chunk-{chunk['index']:06d}.bin"
                _download(client, chunk["artifact_id"], piece, chunk["sha256"], chunk["size_bytes"])
                with piece.open("rb") as source:
                    for block in iter(lambda: source.read(READ_BLOCK), b""):
                        total += len(block)
                        digest.update(block)
                        combined.write(block)
                piece.unlink()
        if total != archive_size or digest.hexdigest() != archive_sha:
            raise ValueError("The reassembled archive failed its checksum")
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            extract_archive(archive_path, staging, files)
            os.replace(staging, target)
            staging = None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
    return {
        "file_count": len(files),
        "restored_files": sum(1 for item in files if item["included_in_archive"]),
        "data_fingerprints": [item["path"] for item in files if item["role"] == "data"],
    }
