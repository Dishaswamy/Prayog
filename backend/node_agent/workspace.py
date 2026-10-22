"""
Preparing a run's workspace on the node (code, data, output), verifying everything fetched.

The node trusts nothing it downloads: each code chunk is checked against the size and SHA-256 the server
recorded, the joined archive against the manifest's digest, every extracted file against its inventory
entry, and every dataset file against its BLAKE3 digest. Any mismatch fails the preparation, and the run
is reported failed rather than started on bytes that are not the ones recorded.

This module uses only the standard library, httpx and blake3, so a node needs nothing else from the backend.
"""

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath

import httpx
from blake3 import blake3

BLOCK = 1024 * 1024
MANIFEST_ENTRY = "snapshot-manifest.json"
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024


class WorkspaceError(Exception):
    """The workload could not be prepared as recorded. The message goes to the run's log."""


def _safe(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    if not path or "\\" in path or p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts) \
            or (p.parts and ":" in p.parts[0]):
        raise WorkspaceError(f"Unsafe path in the workload: {path!r}")
    return p


def _download(http: httpx.Client, url: str, headers: dict, target: Path, hasher, expected_size: int | None) -> int:
    total = 0
    with http.stream("GET", url, headers=headers, timeout=600) as response:
        if response.status_code != 200:
            response.read()
            raise WorkspaceError(f"The server refused {url} (HTTP {response.status_code})")
        with target.open("xb") as out:
            for block in response.iter_bytes(BLOCK):
                total += len(block)
                if expected_size is not None and total > expected_size:
                    raise WorkspaceError(f"{url} sent more bytes than recorded")
                hasher.update(block)
                out.write(block)
    if expected_size is not None and total != expected_size:
        raise WorkspaceError(f"{url} sent {total} bytes; {expected_size} were recorded")
    return total


def fetch_code(http: httpx.Client, headers: dict, allocation_id: int, load: dict, code_dir: Path, scratch: Path) -> None:
    """Download the version's chunks, verify, join, and extract the archive into code_dir."""
    archive_path = scratch / "code.zip"
    archive_hash = hashlib.sha256()
    with archive_path.open("xb") as joined:
        for chunk in load["chunks"]:
            piece = scratch / f"chunk-{chunk['artifact_id']}"
            digest = hashlib.sha256()
            _download(http, f"/agent/allocations/{allocation_id}/code/{chunk['artifact_id']}", headers, piece, digest,
                      chunk["size_bytes"])
            if digest.hexdigest() != chunk["sha256"]:
                raise WorkspaceError("A code chunk failed its checksum")
            with piece.open("rb") as source:
                for block in iter(lambda: source.read(BLOCK), b""):
                    archive_hash.update(block)
                    joined.write(block)
            piece.unlink()
    if archive_hash.hexdigest() != load["archive_sha256"] or archive_path.stat().st_size != load["archive_size_bytes"]:
        raise WorkspaceError("The code archive failed its checksum")
    _extract(archive_path, code_dir, load["files"])


def _extract(archive_path: Path, target: Path, files: list[dict]) -> None:
    expected = {f["path"]: f for f in files if f.get("included_in_archive")}
    seen, total = set(), 0
    target.mkdir(mode=0o755, parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = _safe(info.filename.rstrip("/")).as_posix()
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise WorkspaceError("The code archive holds a link or special file")
            if info.is_dir() or name == MANIFEST_ENTRY:
                continue
            item = expected.get(name)
            if item is None or name in seen:
                raise WorkspaceError(f"The code archive holds an unexpected file: {name}")
            seen.add(name)
            destination = target.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with archive.open(info) as source, destination.open("xb") as out:
                for block in iter(lambda: source.read(BLOCK), b""):
                    size += len(block)
                    total += len(block)
                    if total > MAX_EXTRACTED_BYTES:
                        raise WorkspaceError("The code is larger than the extraction limit")
                    digest.update(block)
                    out.write(block)
            if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                raise WorkspaceError(f"A code file failed its checksum: {name}")
    if seen != set(expected):
        raise WorkspaceError("The code archive is missing files its manifest lists")
    for path in (p for p in target.rglob("*") if p.is_file()):
        os.chmod(path, 0o444)


def fetch_data(http: httpx.Client, headers: dict, allocation_id: int, dataset: list[dict], data_dir: Path) -> None:
    """Download each dataset file to data_dir/<path>, verifying its BLAKE3 digest and size."""
    data_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    for item in dataset:
        relative = _safe(item["path"])
        destination = data_dir.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = blake3()
        _download(http, f"/agent/allocations/{allocation_id}/data/{item['blake3']}", headers, destination, digest,
                  item["size_bytes"])
        if digest.hexdigest() != item["blake3"]:
            destination.unlink(missing_ok=True)
            raise WorkspaceError(f"The dataset file failed its BLAKE3 check: {item['path']}")
        os.chmod(destination, 0o444)


def prepare(http: httpx.Client, headers: dict, allocation_id: int, load: dict, workspace: Path) -> dict[str, Path]:
    """Create workspace/{code,data,output,scratch} and fill code and data. Returns the directories."""
    dirs = {name: workspace / name for name in ("code", "data", "output", "scratch")}
    for name, path in dirs.items():
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chmod(dirs["output"], 0o777)                 # the run's user may be a different uid inside a container
    entrypoint = _safe(load["entrypoint"])
    fetch_code(http, headers, allocation_id, load, dirs["code"], dirs["scratch"])
    if not dirs["code"].joinpath(*entrypoint.parts).is_file():
        raise WorkspaceError(f"The entrypoint {load['entrypoint']} is not in the code")
    fetch_data(http, headers, allocation_id, load["dataset"], dirs["data"])
    return dirs


def dump(load: dict) -> str:
    return json.dumps(load["hyperparameters"], separators=(",", ":"), sort_keys=True)
