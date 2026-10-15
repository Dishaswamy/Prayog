"""
Maintenance windows (GPU requirements section 7.8; FR-GPU-029, 081 to 085, 137, 138).

An admin books a window on selected units. Before confirming, the system shows the
units' clear-by time (when every run on them is guaranteed to have ended) and, if the
window starts earlier, the collision set: the runs it would stop. A window with a
collision set is booked only on explicit confirmation; the admin may instead move or
narrow it. Eligibility already keeps new jobs off units whose window starts before the
job would end (FR-GPU-034, 082); runs still active when a window opens are stopped by
the reaper and requeued (FR-GPU-085).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import AppError, BadRequestError, NotFoundError
from logic.scheduler.claim import utc_now
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from models import User

log = logging.getLogger("prayog.scheduler")


@dataclass(frozen=True)
class Collision:
    job_id: int
    owner: str                    # public user ID
    lab_id: int
    gpu_hours_at_risk: float      # GPU time the run will have used when the window stops it
    guaranteed_end: datetime


@dataclass(frozen=True)
class Preview:
    clear_by: datetime
    collisions: tuple[Collision, ...]


class CollisionNotConfirmed(AppError):
    """The window would stop running jobs and the admin has not confirmed it (FR-GPU-084)."""
    status_code = 409

    def __init__(self, preview: Preview):
        super().__init__(f"The window starts before the units clear at {preview.clear_by:%Y-%m-%d %H:%M}; "
                         f"it would stop {len(preview.collisions)} job(s). Confirm, move or narrow it.")
        self.code = "MAINTENANCE_COLLISION"
        self.preview = preview


def preview(
    db: Session, unit_ids: list[int], starts_at: datetime, now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> Preview:
    """Clear-by time of the units and the runs a window starting at starts_at would stop (FR-GPU-083, 084)."""
    now = now or utc_now()
    _check_units(db, unit_ids)
    bound = timedelta(seconds=cfg.walltime_completion_bound_seconds)
    rows = db.execute(
        select(orm.Job, orm.Allocation.acknowledged_at, User.user_id)
        .join(orm.Allocation, orm.Allocation.job_id == orm.Job.id)
        .join(User, User.id == orm.Job.user_id)
        .where(
            orm.Allocation.released_at.is_(None),
            orm.Allocation.id.in_(select(orm.GpuUnit.current_allocation_id).where(orm.GpuUnit.id.in_(unit_ids))),
        )
        .order_by(orm.Job.id)
    ).all()

    clear_by = now
    collisions = []
    for job, started, owner in rows:
        start = started or now                                   # not started yet: may start now
        end = start + timedelta(minutes=job.walltime_minutes) + bound
        clear_by = max(clear_by, end)
        if end > starts_at:
            hours = max(0.0, (starts_at - start).total_seconds() / 3600) * job.gpus
            collisions.append(Collision(job.id, owner, job.lab_id, round(hours, 4), end))
    return Preview(clear_by, tuple(collisions))


def book(
    db: Session, unit_ids: list[int], starts_at: datetime, ends_at: datetime, reason: str,
    confirm: bool = False, now: datetime | None = None, cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> tuple[str, Preview]:
    """Book one window on the units (FR-GPU-081). Returns the booking ID and the preview it was booked against."""
    now = now or utc_now()
    _check_window(starts_at, ends_at, reason, now)
    p = preview(db, unit_ids, starts_at, now, cfg)
    if p.collisions and not confirm:
        raise CollisionNotConfirmed(p)
    booking_id = str(uuid4())
    db.add_all(orm.MaintenanceWindow(unit_id=u, starts_at=starts_at, ends_at=ends_at, reason=reason.strip(),
                                     booking_id=booking_id) for u in sorted(set(unit_ids)))
    _commit(db)
    _log_collisions(booking_id, p)
    return booking_id, p


def move(
    db: Session, booking_id: str, starts_at: datetime, ends_at: datetime,
    confirm: bool = False, now: datetime | None = None, cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> Preview:
    """Move or narrow a booking, checked against the collision set again (FR-GPU-084)."""
    now = now or utc_now()
    windows = _booking(db, booking_id)
    _check_window(starts_at, ends_at, windows[0].reason, now)
    p = preview(db, [w.unit_id for w in windows], starts_at, now, cfg)
    if p.collisions and not confirm:
        raise CollisionNotConfirmed(p)
    for w in windows:
        w.starts_at, w.ends_at = starts_at, ends_at
    _commit(db)
    _log_collisions(booking_id, p)
    return p


def cancel(db: Session, booking_id: str) -> None:
    """Cancelling restores eligibility at once; runs already stopped stay requeued (FR-GPU-138)."""
    _booking(db, booking_id)
    db.execute(delete(orm.MaintenanceWindow).where(orm.MaintenanceWindow.booking_id == booking_id))
    _commit(db)
    log.info("maintenance booking %s cancelled", booking_id)


@dataclass(frozen=True)
class Booking:
    booking_id: str
    unit_ids: tuple[int, ...]
    starts_at: datetime
    ends_at: datetime
    reason: str


def bookings(db: Session, now: datetime | None = None) -> list[Booking]:
    """Bookings not yet over."""
    now = now or utc_now()
    grouped: dict[str, list[orm.MaintenanceWindow]] = {}
    for w in db.scalars(
        select(orm.MaintenanceWindow).where(orm.MaintenanceWindow.ends_at > now)
        .order_by(orm.MaintenanceWindow.starts_at, orm.MaintenanceWindow.unit_id)
    ):
        grouped.setdefault(w.booking_id or f"window-{w.id}", []).append(w)
    return [Booking(bid, tuple(w.unit_id for w in ws), ws[0].starts_at, ws[0].ends_at, ws[0].reason)
            for bid, ws in grouped.items()]


def _commit(db: Session) -> None:
    try:
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _booking(db: Session, booking_id: str) -> list[orm.MaintenanceWindow]:
    windows = db.scalars(
        select(orm.MaintenanceWindow).where(orm.MaintenanceWindow.booking_id == booking_id).with_for_update()
    ).all()
    if not windows:
        raise NotFoundError(f"Maintenance booking {booking_id} not found.")
    return list(windows)


def _check_units(db: Session, unit_ids: list[int]) -> None:
    if not unit_ids:
        raise BadRequestError("Choose at least one GPU unit.")
    found = set(db.scalars(select(orm.GpuUnit.id).where(orm.GpuUnit.id.in_(unit_ids), orm.GpuUnit.retired_at.is_(None))))
    missing = sorted(set(unit_ids) - found)
    if missing:
        raise BadRequestError(f"Unknown GPU units: {missing}.")


def _check_window(starts_at: datetime, ends_at: datetime, reason: str, now: datetime) -> None:
    if ends_at <= starts_at:
        raise BadRequestError("A maintenance window must end after it starts.")
    if ends_at <= now:
        raise BadRequestError("A maintenance window must end in the future.")
    if not reason or not reason.strip():
        raise BadRequestError("A maintenance window needs a reason.")


def _log_collisions(booking_id: str, p: Preview) -> None:
    for c in p.collisions:
        # Owner notification with the planned stop time and reason is FR-GPU-137 (notification records: FR-GPU-176).
        log.info("maintenance %s will stop job %s of %s", booking_id, c.job_id, c.owner)
