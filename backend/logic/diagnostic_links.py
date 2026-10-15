import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from logic.artifacts import get_verified_artifact
from logic.errors import ConflictError, NotFoundError
from mlflow_models import (
    Artifact,
    DiagnosticAccessAudit,
    DiagnosticLink,
    ModelSnapshot,
    RebuildAttempt,
)

MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 900


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit(
    db: Session,
    user_id: int,
    session_hash: str,
    outcome: str,
    rebuild_id: int | None = None,
    link_id: str | None = None,
) -> None:
    db.add(DiagnosticAccessAudit(
        id=str(uuid4()),
        user_id=user_id,
        rebuild_id=rebuild_id,
        link_id=link_id,
        session_hash=session_hash,
        outcome=outcome,
    ))
    db.commit()


def create_diagnostic_link(
    db: Session,
    rebuild_id: int,
    user,
    session_id: str,
    ttl_seconds: int = 300,
) -> tuple[str, datetime]:
    session_hash = _hash(session_id) if session_id else _hash("missing-session")
    if ttl_seconds < MIN_TTL_SECONDS or ttl_seconds > MAX_TTL_SECONDS:
        _audit(db, user.id, session_hash, "INVALID_TTL", rebuild_id)
        raise ConflictError("Diagnostic link lifetime must be between 60 and 900 seconds")
    if not session_id:
        _audit(db, user.id, session_hash, "SESSION_MISMATCH", rebuild_id)
        raise NotFoundError("Rebuild attempt not found")

    attempt = (
        db.query(RebuildAttempt)
        .filter(RebuildAttempt.id == rebuild_id, RebuildAttempt.user_id == user.id)
        .with_for_update()
        .first()
    )
    if attempt is None:
        _audit(db, user.id, session_hash, "ATTEMPT_NOT_FOUND", rebuild_id)
        raise NotFoundError("Rebuild attempt not found")
    if attempt.state != "REBUILD_FAILED":
        _audit(db, user.id, session_hash, "STATE_REJECTED", rebuild_id)
        raise ConflictError("Diagnostic links are only available after a rebuild failure")
    if not attempt.logs_artifact_id:
        _audit(db, user.id, session_hash, "NO_LOG_ARTIFACT", rebuild_id)
        raise ConflictError("The failed rebuild has no diagnostic log artifact")

    snapshot = db.query(ModelSnapshot).filter(
        ModelSnapshot.snapshot_id == attempt.snapshot_id,
        ModelSnapshot.user_id == user.id,
    ).first()
    if snapshot is None:
        _audit(db, user.id, session_hash, "ATTEMPT_NOT_FOUND", rebuild_id)
        raise NotFoundError("Rebuild attempt not found")
    artifact = db.query(Artifact).filter(
        Artifact.id == attempt.logs_artifact_id,
        Artifact.user_id == user.id,
        Artifact.experiment_id == snapshot.experiment_id,
        Artifact.kind == "LOG",
    ).first()
    if artifact is None:
        _audit(db, user.id, session_hash, "ARTIFACT_MISSING", rebuild_id)
        raise ConflictError("The failed rebuild's diagnostic artifact is unavailable")

    try:
        get_verified_artifact(db, artifact.id, user.id, artifact.experiment_id, "LOG")
    except Exception as exc:
        _audit(db, user.id, session_hash, "ARTIFACT_INTEGRITY_FAILED", rebuild_id)
        raise ConflictError("The failed rebuild's diagnostic artifact is unavailable") from exc

    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    link = DiagnosticLink(
        id=str(uuid4()),
        token_hash=_hash(token),
        session_hash=session_hash,
        user_id=user.id,
        rebuild_id=attempt.id,
        artifact_id=artifact.id,
        expires_at=expires_at,
    )
    db.add(link)
    db.add(DiagnosticAccessAudit(
        id=str(uuid4()),
        user_id=user.id,
        rebuild_id=attempt.id,
        link_id=link.id,
        session_hash=session_hash,
        outcome="CREATED",
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return token, expires_at


def consume_diagnostic_link(
    db: Session,
    token: str,
    user,
    session_id: str,
) -> tuple[Artifact, object]:
    session_hash = _hash(session_id) if session_id else _hash("missing-session")
    if not token or len(token) < 40 or len(token) > 100:
        _audit(db, user.id, session_hash, "INVALID_TOKEN")
        raise NotFoundError("Diagnostic link not found or expired")
    token_hash = _hash(token)
    link = (
        db.query(DiagnosticLink)
        .filter(DiagnosticLink.token_hash == token_hash)
        .with_for_update()
        .first()
    )
    if link is None:
        _audit(db, user.id, session_hash, "INVALID_TOKEN")
        raise NotFoundError("Diagnostic link not found or expired")
    if link.consumed_at is not None:
        _audit(db, user.id, session_hash, "REPLAY", link.rebuild_id, link.id)
        raise NotFoundError("Diagnostic link not found or expired")
    if link.expires_at <= _now():
        _audit(db, user.id, session_hash, "EXPIRED", link.rebuild_id, link.id)
        raise NotFoundError("Diagnostic link not found or expired")
    if link.user_id != user.id:
        _audit(db, user.id, session_hash, "OWNER_MISMATCH", link.rebuild_id, link.id)
        raise NotFoundError("Diagnostic link not found or expired")
    if not session_id or link.session_hash != session_hash:
        _audit(db, user.id, session_hash, "SESSION_MISMATCH", link.rebuild_id, link.id)
        raise NotFoundError("Diagnostic link not found or expired")

    artifact = db.query(Artifact).filter(
        Artifact.id == link.artifact_id,
        Artifact.user_id == link.user_id,
        Artifact.kind == "LOG",
    ).first()
    if artifact is None:
        _audit(db, user.id, session_hash, "ARTIFACT_MISSING", link.rebuild_id, link.id)
        raise NotFoundError("Diagnostic link not found or expired")
    try:
        verified_artifact, path = get_verified_artifact(
            db, artifact.id, link.user_id, artifact.experiment_id, "LOG"
        )
    except Exception as exc:
        _audit(db, user.id, session_hash, "ARTIFACT_INTEGRITY_FAILED", link.rebuild_id, link.id)
        raise NotFoundError("Diagnostic link not found or expired") from exc

    link.consumed_at = _now()
    db.add(DiagnosticAccessAudit(
        id=str(uuid4()),
        user_id=user.id,
        rebuild_id=link.rebuild_id,
        link_id=link.id,
        session_hash=session_hash,
        outcome="CONSUMED",
    ))
    db.commit()
    return verified_artifact, path
