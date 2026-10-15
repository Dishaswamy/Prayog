"""
The reaper: the time-driven half of the lifecycle, run on every tick before the pass
(architecture section 7.6).

    heartbeat lost        node -> DOWN, alert; its allocations released as NODE_FAILURE
                          and the jobs requeued with their original submit time
                          (FR-GPU-077, 079, 135)
    dispatch not acked    released as DISPATCH_TIMEOUT; FAILED after max attempts (FR-GPU-126, 127)
    past walltime         the server-side backstop asks the agent to stop the run; the
                          agent's own timer is the primary (FR-GPU-073, 074, 134)
    window opened         runs on its units stopped as MAINTENANCE and requeued (FR-GPU-085)
    hard drain due        after drain_grace, runs on a hard-drained or hard-decommissioned
                          node stopped as EMERGENCY_DRAIN and requeued (FR-GPU-080, 114)
    decommission done     a DECOMMISSIONING node with no live allocation becomes
                          DECOMMISSIONED and its credential is revoked (FR-GPU-115); then
                          jobs no node can ever run are cancelled (FR-GPU-117)

Every action goes through lifecycle.request_stop / release or a locked node row, so a
reaper racing an agent report or an admin action leaves one consistent result.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import ConflictError
from logic.scheduler.claim import utc_now
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import AlertKind, DrainMode, JobState, NodeState, ReleaseReason
from logic.scheduler.lifecycle import release, request_stop
from logic.scheduler.nodes import cancel_unsatisfiable_jobs, raise_alert, transition

log = logging.getLogger("prayog.scheduler")
WATCHED = (NodeState.AVAILABLE, NodeState.DRAINING, NodeState.DECOMMISSIONING)   # nodes that must heartbeat


@dataclass
class ReapResult:
    nodes_down: list[int] = field(default_factory=list)
    released: dict[int, str] = field(default_factory=dict)        # job id -> release reason
    stops_requested: dict[int, str] = field(default_factory=dict)  # job id -> stop reason
    decommissioned: list[int] = field(default_factory=list)
    cancelled_no_capable_node: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any((self.nodes_down, self.released, self.stops_requested, self.decommissioned,
                    self.cancelled_no_capable_node))


def reap(session: Session, now: datetime | None = None, cfg: SchedulerConfig = DEFAULT_CONFIG) -> ReapResult:
    now = now or utc_now()
    result = ReapResult()
    _heartbeats(session, now, cfg, result)
    _dispatch_timeouts(session, now, cfg, result)
    _walltime_backstop(session, now, cfg, result)
    _maintenance_starts(session, now, cfg, result)
    _hard_drains(session, now, cfg, result)
    _decommissions(session, now, result)
    if result.decommissioned:
        result.cancelled_no_capable_node = cancel_unsatisfiable_jobs(session, now)
    if result.changed:
        log.info("reaper: %s", result)
    return result


def _jobs_on_node(session: Session, node_id: int) -> list[int]:
    return list(session.scalars(
        select(orm.Allocation.job_id)
        .where(orm.Allocation.node_id == node_id, orm.Allocation.released_at.is_(None))
        .order_by(orm.Allocation.job_id)
    ))


def _release(session: Session, job_id: int, reason: ReleaseReason, now: datetime, cfg: SchedulerConfig,
             result: ReapResult) -> None:
    try:
        release(session, job_id, reason, now, cfg=cfg)
        result.released[job_id] = reason
    except ConflictError:
        pass                                 # an agent report or a cancel got there first


def _stop(session: Session, job_id: int, reason: ReleaseReason, now: datetime, cfg: SchedulerConfig,
          result: ReapResult) -> None:
    try:
        stop = request_stop(session, job_id, reason, now, cfg)
        (result.released if stop.released else result.stops_requested)[job_id] = stop.stop_reason
    except ConflictError:
        pass


def _heartbeats(session: Session, now: datetime, cfg: SchedulerConfig, result: ReapResult) -> None:
    deadline = now - timedelta(seconds=cfg.heartbeat_timeout_seconds)
    stale = session.scalars(select(orm.Node.id).where(
        orm.Node.state.in_(WATCHED),
        orm.Node.last_heartbeat_at < deadline,        # nodes reach AVAILABLE only through a heartbeat
    )).all()
    for node_id in stale:
        node = session.scalars(
            select(orm.Node).where(orm.Node.id == node_id).with_for_update().execution_options(populate_existing=True)
        ).one()
        # Re-check under the lock: a heartbeat may have arrived meanwhile.
        if node.state not in WATCHED or node.last_heartbeat_at >= deadline:  # pragma: no cover - only a racing heartbeat
            session.rollback()
            continue
        if node.state != NodeState.DECOMMISSIONING:      # a retiring node stays retiring; its work is lost either way
            transition(node, NodeState.DOWN, now, "heartbeat lost")
            raise_alert(session, node.id, AlertKind.NODE_DOWN,
                        f"No heartbeat since {node.last_heartbeat_at:%Y-%m-%d %H:%M:%S}.", now)
            result.nodes_down.append(node.id)
        session.commit()
        for job_id in _jobs_on_node(session, node_id):
            _release(session, job_id, ReleaseReason.NODE_FAILURE, now, cfg, result)


def _dispatch_timeouts(session: Session, now: datetime, cfg: SchedulerConfig, result: ReapResult) -> None:
    deadline = now - timedelta(seconds=cfg.dispatch_ack_timeout_seconds)
    for job_id in session.scalars(select(orm.Allocation.job_id).where(
        orm.Allocation.released_at.is_(None), orm.Allocation.acknowledged_at.is_(None),
        orm.Allocation.claimed_at < deadline,
    ).order_by(orm.Allocation.job_id)).all():
        _release(session, job_id, ReleaseReason.DISPATCH_TIMEOUT, now, cfg, result)


def _walltime_backstop(session: Session, now: datetime, cfg: SchedulerConfig, result: ReapResult) -> None:
    tolerance = timedelta(seconds=cfg.walltime_tolerance_seconds)
    rows = session.execute(
        select(orm.Allocation.job_id, orm.Allocation.acknowledged_at, orm.Job.walltime_minutes)
        .join(orm.Job, orm.Job.id == orm.Allocation.job_id)
        .where(orm.Allocation.released_at.is_(None), orm.Allocation.acknowledged_at.is_not(None),
               orm.Allocation.stop_requested_at.is_(None), orm.Job.state == JobState.RUNNING)
    ).all()
    for job_id, started, walltime in rows:
        if now >= started + timedelta(minutes=walltime) + tolerance:
            _stop(session, job_id, ReleaseReason.WALLTIME_EXCEEDED, now, cfg, result)


def _maintenance_starts(session: Session, now: datetime, cfg: SchedulerConfig, result: ReapResult) -> None:
    open_window = select(orm.MaintenanceWindow.unit_id).where(
        orm.MaintenanceWindow.starts_at <= now, orm.MaintenanceWindow.ends_at > now
    )
    jobs = session.scalars(
        select(orm.Allocation.job_id).distinct()
        .join(orm.GpuUnit, orm.GpuUnit.current_allocation_id == orm.Allocation.id)
        .where(orm.GpuUnit.id.in_(open_window), orm.Allocation.released_at.is_(None),
               orm.Allocation.stop_requested_at.is_(None))
    ).all()
    for job_id in sorted(jobs):
        _stop(session, job_id, ReleaseReason.MAINTENANCE, now, cfg, result)


def _hard_drains(session: Session, now: datetime, cfg: SchedulerConfig, result: ReapResult) -> None:
    due = now - timedelta(minutes=cfg.drain_grace_minutes)
    nodes = session.scalars(select(orm.Node.id).where(
        orm.Node.state.in_((NodeState.DRAINING, NodeState.DECOMMISSIONING)),
        orm.Node.drain_mode == DrainMode.HARD,
        orm.Node.state_changed_at <= due,
    )).all()
    for node_id in nodes:
        for job_id in session.scalars(select(orm.Allocation.job_id).where(
            orm.Allocation.node_id == node_id, orm.Allocation.released_at.is_(None),
            orm.Allocation.stop_requested_at.is_(None),
        )).all():
            _stop(session, job_id, ReleaseReason.EMERGENCY_DRAIN, now, cfg, result)


def _decommissions(session: Session, now: datetime, result: ReapResult) -> None:
    live = select(orm.Allocation.id).where(
        and_(orm.Allocation.node_id == orm.Node.id, orm.Allocation.released_at.is_(None))
    ).exists()
    for node_id in session.scalars(
        select(orm.Node.id).where(orm.Node.state == NodeState.DECOMMISSIONING, ~live)
    ).all():
        node = session.scalars(
            select(orm.Node).where(orm.Node.id == node_id).with_for_update().execution_options(populate_existing=True)
        ).one()
        still_live = session.scalar(select(orm.Allocation.id).where(
            orm.Allocation.node_id == node_id, orm.Allocation.released_at.is_(None)
        ).limit(1))
        if node.state != NodeState.DECOMMISSIONING or still_live is not None:  # pragma: no cover - only a racing action
            session.rollback()
            continue
        # Run artifacts committed to the content store is the other condition (FR-GPU-115); the
        # store is not built yet, so a released allocation is taken as complete.
        transition(node, NodeState.DECOMMISSIONED, now, node.state_reason)
        node.credential_revoked_at = now
        node.drain_mode = None
        session.commit()
        result.decommissioned.append(node_id)
