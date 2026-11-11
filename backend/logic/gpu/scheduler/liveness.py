"""
Was the platform able to hear from nodes? (Defect D3-08; FR-GPU-077.)

A node's silence means it has failed only if the platform was listening. When the API or
the database is down, heartbeats cannot be recorded, so every healthy node looks dead, and
the reaper used to mark them all DOWN and requeue their running jobs.

Each API process stamps one row every few seconds. The reaper judges silence only when the
stamp is fresh and the API has been up for a full heartbeat timeout since its last gap, so
every node has had its chance to report. Before any API has stamped, nothing changes.
"""

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

import models as orm
from logic.gpu.scheduler.claim import utc_now
from logic.gpu.scheduler.config import DEFAULT_CONFIG, SchedulerConfig

log = logging.getLogger("prayog.scheduler")


def _gap(cfg: SchedulerConfig) -> timedelta:
    return timedelta(seconds=3 * cfg.platform_stamp_seconds)


def stamp(session: Session, now: datetime | None = None, cfg: SchedulerConfig = DEFAULT_CONFIG) -> None:
    """Record that the API is up. After a gap, listening starts again from now."""
    now = now or utc_now()
    table = orm.PlatformLiveness.__table__
    statement = insert(table).values(id=1, api_seen_at=now, listening_since=now)
    after_a_gap = table.c.api_seen_at < now - _gap(cfg)
    session.execute(statement.on_conflict_do_update(
        index_elements=[table.c.id],
        set_={"api_seen_at": now, "listening_since": case((after_a_gap, now), else_=table.c.listening_since)},
    ))
    session.commit()


def can_judge_silence(session: Session, now: datetime, cfg: SchedulerConfig = DEFAULT_CONFIG) -> bool:
    """False while the API is down, and for one heartbeat timeout after it comes back."""
    row = session.execute(select(orm.PlatformLiveness.api_seen_at, orm.PlatformLiveness.listening_since)).first()
    if row is None:
        return True                               # no API has stamped: tools and tests that reap on their own
    if row.api_seen_at < now - _gap(cfg):
        return False
    return row.listening_since <= now - timedelta(seconds=cfg.heartbeat_timeout_seconds)


def start_stamping(sessions: sessionmaker, cfg: SchedulerConfig = DEFAULT_CONFIG) -> threading.Event:
    """Stamp from a daemon thread until the returned event is set. A database outage only pauses it."""
    stop = threading.Event()

    def run() -> None:
        failing = False
        while not stop.is_set():
            try:
                with sessions() as session:
                    stamp(session, cfg=cfg)
                failing = False
            except Exception:
                if not failing:                   # once per outage; the handlers report the requests it fails
                    log.warning("could not stamp platform liveness; retrying", exc_info=True)
                failing = True
            stop.wait(cfg.platform_stamp_seconds)

    threading.Thread(target=run, name="platform-liveness", daemon=True).start()
    return stop
