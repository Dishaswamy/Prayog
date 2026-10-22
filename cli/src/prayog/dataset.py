"""Datasets for a run: scan a file or folder, fingerprint each file with BLAKE3, and stage what the server lacks.

A dataset is not kept. It is staged for the run so a GPU node can read it, and the server deletes it a fixed
time after the run ends. What the run records for good is each file's path, size and BLAKE3 digest. Staging is
by content: a file the server already holds (from an earlier run) is not sent again.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from blake3 import blake3

from prayog.client import Client
from prayog.errors import CliError, USAGE
from prayog.folder import EXCLUDED_DIRS, _is_secret, safe_relative_path

MAX_FILES = 20000
BLOCK = 16 * 1024 * 1024


@dataclass(frozen=True)
class DataFile:
    path: str                  # relative, as the run will see it under its data directory
    size_bytes: int
    blake3: str
    source: Path


def blake3_of(path: Path) -> str:
    digest = blake3(max_threads=blake3.AUTO)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def scan(target: Path) -> list[DataFile]:
    """Every file of a dataset. A single file is staged under its own name; a folder under its relative paths.
    Secrets and tool/VCS directories are skipped, and symbolic links are refused."""
    if target.is_symlink():
        raise CliError(f"'{target}' is a symbolic link; give the real path.", USAGE)
    if target.is_file():
        return [DataFile(target.name, target.stat().st_size, blake3_of(target), target)]
    if not target.is_dir():
        raise CliError(f"'{target}' is not a file or folder.", USAGE)
    root = target.resolve()
    found: list[DataFile] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        kept = []
        for name in sorted(directories):
            if (here / name).is_symlink():
                raise CliError(f"The dataset contains a symbolic link: {here / name}", USAGE)
            if name.lower() not in EXCLUDED_DIRS:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            path = here / name
            if path.is_symlink():
                raise CliError(f"The dataset contains a symbolic link: {path}", USAGE)
            if _is_secret(name, path) or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                safe_relative_path(relative)
            except ValueError as exc:
                raise CliError(str(exc), USAGE) from exc
            found.append(DataFile(relative, path.stat().st_size, blake3_of(path), path))
            if len(found) > MAX_FILES:
                raise CliError(f"The dataset has more than {MAX_FILES} files.", USAGE)
    if not found:
        raise CliError(f"'{target}' has no files to stage.", USAGE)
    found.sort(key=lambda item: item.path)
    return found


def combine(targets: list[Path]) -> list[DataFile]:
    """Scan several datasets into one, refusing two files that would land on the same path."""
    combined: dict[str, DataFile] = {}
    for target in targets:
        for item in scan(target):
            if item.path in combined:
                raise CliError(f"Two dataset files would both be '{item.path}'.", USAGE)
            combined[item.path] = item
    return sorted(combined.values(), key=lambda item: item.path)


def _stream(path: Path) -> Iterator[bytes]:
    with path.open("rb") as source:
        for block in iter(lambda: source.read(BLOCK), b""):
            yield block


def stage(client: Client, files: list[DataFile], say: Optional[Callable[[str], None]] = None) -> tuple[int, int]:
    """Upload the files the server does not hold. Returns (uploaded, already_there). A file that changed on
    disk since it was fingerprinted is refused by the server's check, not silently staged."""
    say = say or (lambda _: None)
    unique = {(f.blake3, f.size_bytes): f for f in files}
    reply = client.post("/mlflow/staging/missing",
                        json={"files": [{"blake3": d, "size_bytes": s} for d, s in unique]})
    missing = set(reply["missing"])
    sent = 0
    for item in (f for f in unique.values() if f.blake3 in missing):
        say(f"  uploading {item.path} ({human(item.size_bytes)})")
        client.upload(f"/mlflow/staging/files/{item.blake3}", {"size": item.size_bytes}, _stream(item.source), method="PUT")
        sent += 1
    return sent, len(unique) - sent


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
