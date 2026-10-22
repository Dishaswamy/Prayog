"""
Temporary dataset staging.

Datasets are not kept. To train on one, the user stages its files here for the run: a GPU node reads
them, and the bytes are deleted a fixed time after the last run that used them ends (default two
days). What stays for good is each run's fingerprint of the data (RunDatasetFile: path, size, BLAKE3).

Files are content-addressed by BLAKE3 and private to their owner: two users staging identical bytes
each get their own StagedFile row, and nothing tells one of them the other holds it. The bytes of an
identical object are shared in storage, so an object is only removed once no artifact and no live
staged file names it.
"""

import os
from datetime import datetime, timedelta, timezone

import anyio
from blake3 import blake3
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from uuid import uuid4

from logic import artifact_storage
from logic.errors import BadRequestError
from mlflow_models import Artifact, ModelRun, RunDatasetFile, StagedFile

DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_RETENTION_HOURS = 48
MAX_FILES_PER_CHECK = 20000
TERMINAL_RUN_STATES = ("SUCCEEDED", "FAILED", "CANCELLED")


def retention() -> timedelta:
    """How long staged data outlives the last run that used it (PRAYOG_STAGING_RETENTION_HOURS, default 48)."""
    try:
        hours = float(os.getenv("PRAYOG_STAGING_RETENTION_HOURS", str(DEFAULT_RETENTION_HOURS)))
    except ValueError:
        hours = DEFAULT_RETENTION_HOURS
    return timedelta(hours=max(hours, 0.0))


def max_file_bytes() -> int:
    try:
        return int(os.getenv("PRAYOG_STAGING_MAX_FILE_BYTES", str(DEFAULT_MAX_FILE_BYTES)))
    except ValueError:
        return DEFAULT_MAX_FILE_BYTES


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def missing_files(db: Session, user_id: int, files: list[tuple[str, int]]) -> list[str]:
    """The digests (of the given (blake3, size) pairs) the user has not got staged right now."""
    if len(files) > MAX_FILES_PER_CHECK:
        raise BadRequestError("Too many files in one check")
    for digest, size in files:
        if not is_digest(digest) or size < 0:
            raise BadRequestError("Each file needs a BLAKE3 digest and a size")
    held = {
        (row.blake3, row.size_bytes) for row in db.query(StagedFile).filter(
            StagedFile.user_id == user_id, StagedFile.deleted_at.is_(None),
            StagedFile.blake3.in_([digest for digest, _ in files]),
        )
    }
    return sorted({digest for digest, size in files if (digest, size) not in held})


async def store_staged_file(db: Session, user, digest: str, size: int, request) -> tuple[StagedFile, bool]:
    """Receive the bytes, check they are exactly the declared file, and stage them. Returns
    (row, created); re-staging a file that was deleted brings it back."""
    if not is_digest(digest) or size < 0:
        raise BadRequestError("A BLAKE3 digest and a size are required")
    limit = max_file_bytes()
    if size > limit:
        raise HTTPException(status_code=413, detail=f"A staged file may be at most {limit} bytes")
    root = artifact_storage.ARTIFACT_ROOT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    temporary = root / f".{uuid4().hex}.staging"
    hasher, total = blake3(), 0
    try:
        async with await anyio.open_file(temporary, "xb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > size:
                    raise BadRequestError("More bytes arrived than the declared size")
                hasher.update(chunk)
                await output.write(chunk)
        if total != size:
            raise BadRequestError("Fewer bytes arrived than the declared size")
        if hasher.hexdigest() != digest:
            raise BadRequestError("The bytes do not match the declared BLAKE3 digest")
        artifact_storage.persist_artifact(temporary, digest)
        row = db.query(StagedFile).filter(StagedFile.user_id == user.id, StagedFile.blake3 == digest) \
            .with_for_update().first()
        created = row is None or row.deleted_at is not None
        if row is None:
            row = StagedFile(user_id=user.id, blake3=digest, size_bytes=size)
            db.add(row)
        else:
            row.size_bytes, row.deleted_at = size, None
            if created:
                row.created_at = _now()
        db.commit()
        db.refresh(row)
        return row, created
    except Exception:
        db.rollback()
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _object_in_use(db: Session, digest: str, excluding_id: int) -> bool:
    """Does any artifact, or any other live staged file, still name this object?"""
    if db.query(Artifact.id).filter(Artifact.storage_key == digest).first() is not None:
        return True
    return db.query(StagedFile.id).filter(
        StagedFile.blake3 == digest, StagedFile.deleted_at.is_(None), StagedFile.id != excluding_id
    ).first() is not None


def _delete(db: Session, row: StagedFile, now: datetime) -> None:
    row.deleted_at = now
    db.flush()
    if not _object_in_use(db, row.blake3, row.id):
        artifact_storage.delete_content_object(row.blake3)


def _in_active_run(db: Session, staged_id: int):
    return db.query(RunDatasetFile.id).join(ModelRun, ModelRun.id == RunDatasetFile.run_id).filter(
        RunDatasetFile.staged_file_id == staged_id, ModelRun.status.not_in(TERMINAL_RUN_STATES)
    ).first() is not None


def expire_staged_files(db: Session, now: datetime | None = None, hold: timedelta | None = None) -> int:
    """Delete staged files whose retention has passed. A file is kept while any run using it is
    unfinished, and for `hold` after the later of its last upload and the end of the last run that used it. Returns how many files were deleted."""
    now = now or _now()
    hold = retention() if hold is None else hold
    last_use = (
        db.query(RunDatasetFile.staged_file_id, func.max(ModelRun.finished_at).label("last_end"),
                 func.count().filter(ModelRun.status.not_in(TERMINAL_RUN_STATES)).label("active"))
        .join(ModelRun, ModelRun.id == RunDatasetFile.run_id)
        .filter(RunDatasetFile.staged_file_id.is_not(None))
        .group_by(RunDatasetFile.staged_file_id).subquery()
    )
    candidates = (
        db.query(StagedFile, last_use.c.last_end, last_use.c.active)
        .outerjoin(last_use, last_use.c.staged_file_id == StagedFile.id)
        .filter(StagedFile.deleted_at.is_(None))
        .with_for_update(of=StagedFile)
        .all()
    )
    deleted = 0
    for row, last_end, active in candidates:
        if active:
            continue
        since = max(t for t in (last_end, row.created_at) if t is not None)   # a re-upload restarts the clock
        if since + hold <= now:
            _delete(db, row, now)
            deleted += 1
    db.commit()
    return deleted


def clear_project_staging(db: Session, user_id: int, project_id: str) -> dict:
    """Delete now the staged files this project's runs used, unless an unfinished run still needs them."""
    ids = [row[0] for row in db.query(RunDatasetFile.staged_file_id).join(ModelRun, ModelRun.id == RunDatasetFile.run_id)
           .filter(ModelRun.project_id == project_id, ModelRun.user_id == user_id,
                   RunDatasetFile.staged_file_id.is_not(None)).distinct()]
    deleted = kept = freed = 0
    now = _now()
    for row in db.query(StagedFile).filter(StagedFile.id.in_(ids), StagedFile.user_id == user_id,
                                           StagedFile.deleted_at.is_(None)).with_for_update():
        if _in_active_run(db, row.id):
            kept += 1
            continue
        freed += row.size_bytes
        _delete(db, row, now)
        deleted += 1
    db.commit()
    return {"deleted_files": deleted, "kept_in_use": kept, "freed_bytes": freed}
