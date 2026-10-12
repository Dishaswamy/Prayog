import hashlib
import os
from pathlib import Path
from uuid import uuid4

import anyio
from fastapi import HTTPException
from sqlalchemy.orm import Session

from logic.artifact_storage import ARTIFACT_ROOT, delete_artifact, persist_artifact, resolve_artifact
from logic.errors import BadRequestError, NotFoundError
from mlflow_models import Artifact

MAX_ARTIFACT_BYTES = 250 * 1024 * 1024


async def store_artifact(db: Session, user, experiment, kind: str, request) -> Artifact:
    if kind not in {"CODE", "WEIGHTS", "LOG"}:
        raise BadRequestError("Artifact kind must be CODE, WEIGHTS, or LOG")
    ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ARTIFACT_ROOT, 0o700)
    artifact_id = str(uuid4())
    storage_key = str(uuid4())
    temporary_path = ARTIFACT_ROOT / f".{storage_key}.{uuid4().hex}.upload"
    digest = hashlib.sha256()
    total = 0
    persisted = False
    try:
        async with await anyio.open_file(temporary_path, "xb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    raise HTTPException(status_code=413, detail="Artifact exceeds the 250 MiB limit")
                digest.update(chunk)
                await output.write(chunk)
        if total == 0:
            raise BadRequestError("Artifact upload cannot be empty")
        persist_artifact(temporary_path, storage_key)
        persisted = True
        artifact = Artifact(
            id=artifact_id,
            user_id=user.id,
            experiment_id=experiment.id,
            project_id=experiment.project_id,
            model_id=experiment.model_id,
            kind=kind,
            storage_key=storage_key,
            sha256=digest.hexdigest(),
            size_bytes=total,
        )
        db.add(artifact)
        db.commit()
        db.refresh(artifact)
        return artifact
    except Exception:
        db.rollback()
        temporary_path.unlink(missing_ok=True)
        if persisted:
            try:
                delete_artifact(storage_key)
            except Exception:
                pass
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
        raise BadRequestError("Artifact data is missing; snapshot cannot be used") from exc
    if not path.is_file():
        raise BadRequestError("Artifact data is missing; snapshot cannot be used")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
        raise BadRequestError("Artifact integrity verification failed")
    return artifact, path
