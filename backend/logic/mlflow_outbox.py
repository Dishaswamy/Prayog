from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session

from logic.artifacts import get_verified_artifact
from logic.errors import BadRequestError, NotFoundError
from mlflow_models import Artifact, MLFlowOutboxEvent, ModelSnapshot, RebuildAttempt

DEFAULT_MAX_ATTEMPTS = 5
MAX_ERROR_LENGTH = 500


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enqueue_rebuild_terminal_event(db: Session, attempt, snapshot, execution, user_id: int, state: str, log_artifact):
    if state == "REBUILD_FAILED":
        if log_artifact is None:
            return None
        event_type = "STEWARD_DIAGNOSIS_REQUESTED"
        event_key = f"rebuild:{attempt.id}:steward-diagnosis"
    elif state == "REBUILD_SUCCESS":
        event_type = "OPTIMIZATION_REQUESTED"
        event_key = f"rebuild:{attempt.id}:optimization"
    else:
        return None

    event = MLFlowOutboxEvent(
        id=str(uuid4()),
        event_key=event_key,
        event_type=event_type,
        aggregate_id=attempt.id,
        user_id=user_id,
        payload={
            "rebuild_id": attempt.id,
            "snapshot_id": snapshot.snapshot_id,
            "execution_run_id": execution.id,
            "source_snapshot_id": snapshot.snapshot_id,
            "terminal_state": state,
            "log_artifact_id": log_artifact.id if event_type == "STEWARD_DIAGNOSIS_REQUESTED" else None,
        },
        status="PENDING",
        attempts=0,
        available_at=_now(),
    )
    db.add(event)
    return event


def claim_events(db: Session, worker_id: str, limit: int = 10, lease_seconds: int = 60):
    if not worker_id or len(worker_id) > 80:
        raise ValueError("worker_id must contain 1–80 characters")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not 10 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 10 and 3600")

    now = _now()
    rows = (
        db.query(MLFlowOutboxEvent)
        .filter(
            MLFlowOutboxEvent.status.in_(( "PENDING", "PROCESSING")),
            MLFlowOutboxEvent.available_at <= now,
            or_(
                MLFlowOutboxEvent.status == "PENDING",
                MLFlowOutboxEvent.locked_until.is_(None),
                MLFlowOutboxEvent.locked_until <= now,
            ),
        )
        .order_by(MLFlowOutboxEvent.created_at, MLFlowOutboxEvent.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    for event in rows:
        event.status = "PROCESSING"
        event.locked_by = worker_id
        event.locked_until = now + timedelta(seconds=lease_seconds)
        event.attempts += 1
    db.commit()
    return rows


def mark_delivered(db: Session, event_id: str, worker_id: str):
    event = (
        db.query(MLFlowOutboxEvent)
        .filter(
            MLFlowOutboxEvent.id == event_id,
            MLFlowOutboxEvent.status == "PROCESSING",
            MLFlowOutboxEvent.locked_by == worker_id,
            MLFlowOutboxEvent.locked_until > _now(),
        )
        .with_for_update()
        .first()
    )
    if event is None:
        return False
    event.status = "DELIVERED"
    event.delivered_at = _now()
    event.locked_by = None
    event.locked_until = None
    event.last_error = None
    db.commit()
    return True


def mark_delivery_failed(
    db: Session,
    event_id: str,
    worker_id: str,
    error: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
):
    if not 1 <= max_attempts <= 20:
        raise ValueError("max_attempts must be between 1 and 20")
    event = (
        db.query(MLFlowOutboxEvent)
        .filter(
            MLFlowOutboxEvent.id == event_id,
            MLFlowOutboxEvent.status == "PROCESSING",
            MLFlowOutboxEvent.locked_by == worker_id,
            MLFlowOutboxEvent.locked_until > _now(),
        )
        .with_for_update()
        .first()
    )
    if event is None:
        return False
    event.last_error = (error or "Delivery failed")[:MAX_ERROR_LENGTH]
    event.locked_by = None
    event.locked_until = None
    if event.attempts >= max_attempts:
        event.status = "DEAD"
    else:
        event.status = "PENDING"
        delay = min(300, 2 ** min(event.attempts, 8))
        event.available_at = _now() + timedelta(seconds=delay)
    db.commit()
    return True


def _still_eligible(db: Session, event: MLFlowOutboxEvent) -> bool:
    attempt = (
        db.query(RebuildAttempt)
        .filter(
            RebuildAttempt.id == event.aggregate_id,
            RebuildAttempt.user_id == event.user_id,
        )
        .first()
    )
    if attempt is None or attempt.id != event.payload.get("rebuild_id"):
        return False
    if event.event_type == "STEWARD_DIAGNOSIS_REQUESTED":
        if (
            attempt.state != "REBUILD_FAILED"
            or event.payload.get("terminal_state") != "REBUILD_FAILED"
            or event.payload.get("snapshot_id") != attempt.snapshot_id
            or not attempt.logs_artifact_id
        ):
            return False
        if attempt.logs_artifact_id != event.payload.get("log_artifact_id"):
            return False
        snapshot = (
            db.query(ModelSnapshot)
            .filter(
                ModelSnapshot.snapshot_id == attempt.snapshot_id,
                ModelSnapshot.user_id == event.user_id,
            )
            .first()
        )
        if snapshot is None:
            return False
        artifact = (
            db.query(Artifact)
            .filter(
                Artifact.id == attempt.logs_artifact_id,
                Artifact.user_id == event.user_id,
                Artifact.experiment_id == snapshot.experiment_id,
                Artifact.kind == "LOG",
            )
            .first()
        )
        if artifact is None:
            return False
        try:
            get_verified_artifact(db, artifact.id, event.user_id, artifact.experiment_id, "LOG")
        except (BadRequestError, NotFoundError, OSError):
            return False
        return True
    if event.event_type == "OPTIMIZATION_REQUESTED":
        return (
            attempt.state == "REBUILD_SUCCESS"
            and event.payload.get("terminal_state") == "REBUILD_SUCCESS"
            and event.payload.get("snapshot_id") == attempt.snapshot_id
        )
    return False


def dispatch_once(db: Session, worker_id: str, handlers: dict, limit: int = 10, lease_seconds: int = 60):
    """Deliver eligible events via injected handlers; transport and service auth stay outside the state machine."""
    outcomes = []
    for event in claim_events(db, worker_id, limit, lease_seconds):
        event_id = event.id
        event_type = event.event_type
        event_key = event.event_key
        payload = dict(event.payload)
        handler = handlers.get(event_type)
        try:
            if not _still_eligible(db, event):
                changed = mark_delivery_failed(
                    db, event_id, worker_id, "Persisted lifecycle state or required artifact is no longer eligible.", max_attempts=1
                )
                outcomes.append((event_id, "INELIGIBLE" if changed else "LEASE_LOST"))
                continue
            if handler is None:
                raise RuntimeError(f"No handler configured for {event_type}")
            handler(event_key, payload)
            delivered = mark_delivered(db, event_id, worker_id)
            outcomes.append((event_id, "DELIVERED" if delivered else "LEASE_LOST"))
        except Exception as exc:
            db.rollback()
            changed = mark_delivery_failed(db, event_id, worker_id, f"{type(exc).__name__}: handler delivery failed")
            outcomes.append((event_id, "RETRY_SCHEDULED" if changed else "LEASE_LOST"))
    return outcomes
