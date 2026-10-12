import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from logic.artifacts import get_verified_artifact
from logic.errors import ConflictError, NotFoundError
from mlflow_models import Artifact, DiagnosticLink, ModelSnapshot, RebuildAttempt

MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 900


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_diagnostic_link(db: Session, rebuild_id: int, user, ttl_seconds: int = 300) -> tuple[str, datetime]:
    if ttl_seconds < MIN_TTL_SECONDS or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError("Diagnostic link lifetime must be between 60 and 900 seconds")

    attempt = (
        db.query(RebuildAttempt)
        .filter(RebuildAttempt.id == rebuild_id, RebuildAttempt.user_id == user.id)
        .with_for_update()
        .first()
    )
    if attempt is None:
        raise NotFoundError("Rebuild attempt not found")
    if attempt.state != "REBUILD_FAILED":
        raise ConflictError("Diagnostic links are only available after a rebuild failure")
    if not attempt.logs_artifact_id:
        raise ConflictError("The failed rebuild has no diagnostic log artifact")

    snapshot = db.query(ModelSnapshot).filter(
        ModelSnapshot.snapshot_id == attempt.snapshot_id,
        ModelSnapshot.user_id == user.id,
    ).first()
    if snapshot is None:
        raise NotFoundError("Rebuild attempt not found")
    artifact = db.query(Artifact).filter(
        Artifact.id == attempt.logs_artifact_id,
        Artifact.user_id == user.id,
        Artifact.experiment_id == snapshot.experiment_id,
        Artifact.kind == "LOG",
    ).first()
    if artifact is None:
        raise ConflictError("The failed rebuild's diagnostic artifact is unavailable")

    get_verified_artifact(db, artifact.id, user.id, artifact.experiment_id, "LOG")
    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    link = DiagnosticLink(
        id=str(uuid4()),
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        user_id=user.id,
        rebuild_id=attempt.id,
        artifact_id=artifact.id,
        expires_at=expires_at,
    )
    try:
        db.add(link)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return token, expires_at


def consume_diagnostic_link(db: Session, token: str) -> tuple[Artifact, object]:
    if not token or len(token) < 40 or len(token) > 100:
        raise NotFoundError("Diagnostic link not found or expired")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    link = (
        db.query(DiagnosticLink)
        .filter(
            DiagnosticLink.token_hash == token_hash,
            DiagnosticLink.expires_at > _now(),
            DiagnosticLink.consumed_at.is_(None),
        )
        .with_for_update()
        .first()
    )
    if link is None:
        raise NotFoundError("Diagnostic link not found or expired")

    artifact = db.query(Artifact).filter(
        Artifact.id == link.artifact_id,
        Artifact.user_id == link.user_id,
        Artifact.kind == "LOG",
    ).first()
    if artifact is None:
        raise NotFoundError("Diagnostic link not found or expired")
    verified_artifact, path = get_verified_artifact(
        db, artifact.id, link.user_id, artifact.experiment_id, "LOG"
    )
    link.consumed_at = _now()
    db.commit()
    return verified_artifact, path
