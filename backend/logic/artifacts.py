import hashlib
import os
from pathlib import Path
from uuid import uuid4

import anyio
from blake3 import blake3
from fastapi import HTTPException
from sqlalchemy.orm import Session

from logic.artifact_storage import ARTIFACT_ROOT, persist_artifact, resolve_artifact
from logic.errors import BadRequestError, NotFoundError
from mlflow_models import Artifact, Experiment

MAX_ARTIFACT_BYTES = 250 * 1024 * 1024


async def store_artifact(db: Session, user, experiment, kind: str, request) -> Artifact:
    if kind not in {"CODE", "WEIGHTS", "LOG"}:
        raise BadRequestError("Artifact kind must be CODE, WEIGHTS, or LOG")
    ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ARTIFACT_ROOT, 0o700)
    artifact_id = str(uuid4())
    temporary_path = ARTIFACT_ROOT / f".{uuid4().hex}.upload"
    sha256_digest = hashlib.sha256()
    content_digest = blake3()
    total = 0
    try:
        async with await anyio.open_file(temporary_path, "xb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    raise HTTPException(status_code=413, detail="Artifact exceeds the 250 MiB limit")
                sha256_digest.update(chunk)
                content_digest.update(chunk)
                await output.write(chunk)
        if total == 0:
            raise BadRequestError("Artifact upload cannot be empty")

        # Serialize uploads within an experiment so concurrent identical uploads
        # cannot create duplicate metadata rows for the same owner/experiment/kind.
        locked_experiment = (
            db.query(Experiment)
            .filter(Experiment.id == experiment.id, Experiment.user_id == user.id)
            .with_for_update()
            .first()
        )
        if locked_experiment is None:
            raise NotFoundError("Experiment not found")

        storage_key = content_digest.hexdigest()
        existing = db.query(Artifact).filter(
            Artifact.user_id == user.id,
            Artifact.experiment_id == experiment.id,
            Artifact.kind == kind,
            Artifact.storage_key == storage_key,
            Artifact.sha256 == sha256_digest.hexdigest(),
            Artifact.size_bytes == total,
        ).first()
        if isinstance(existing, Artifact):
            # Do not silently return a stale/corrupt record on a duplicate upload.
            get_verified_artifact(db, existing.id, user.id, experiment.id, kind)
            return existing

        # BLAKE3 is the immutable storage identity, shared across metadata rows
        # and namespaces. SHA-256 remains in metadata for compatibility.
        persist_artifact(temporary_path, storage_key)
        artifact = Artifact(
            id=artifact_id,
            user_id=user.id,
            experiment_id=experiment.id,
            project_id=experiment.project_id,
            model_id=experiment.model_id,
            kind=kind,
            storage_key=storage_key,
            sha256=sha256_digest.hexdigest(),
            size_bytes=total,
        )
        db.add(artifact)
        db.commit()
        db.refresh(artifact)
        return artifact
    except Exception:
        db.rollback()
        # Do not delete the object on a metadata failure: the object may be
        # shared by another artifact row with the same content address. Orphans
        # are safe and can be reclaimed later by reference-aware garbage collection.
        raise
    finally:
        temporary_path.unlink(missing_ok=True)


def get_verified_artifact(
    db: Session,
    artifact_id: str,
    user_id: int,
    experiment_id: int,
    kind: str | None = None,
) -> tuple[Artifact, Path]:
    artifact = db.query(Artifact).filter(
        Artifact.id == artifact_id,
        Artifact.user_id == user_id,
        Artifact.experiment_id == experiment_id,
    ).first()
    if artifact is None:
        raise NotFoundError("Artifact not found")
    if kind is not None and artifact.kind != kind:
        raise BadRequestError(f"Expected a {kind} artifact")
    try:
        path = resolve_artifact(artifact.storage_key)
    except Exception as exc:
        raise BadRequestError("Artifact data is missing or corrupt; snapshot cannot be used") from exc
    if not path.is_file():
        raise BadRequestError("Artifact data is missing; snapshot cannot be used")
    sha256_digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha256_digest.update(chunk)
            size += len(chunk)
    if size != artifact.size_bytes or sha256_digest.hexdigest() != artifact.sha256:
        raise BadRequestError("Artifact integrity verification failed")
    if len(artifact.storage_key) == 64 and content_digest_for_path(path) != artifact.storage_key:
        raise BadRequestError("Artifact content address verification failed")
    return artifact, path


def content_digest_for_path(path: Path) -> str:
    digest = blake3()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
