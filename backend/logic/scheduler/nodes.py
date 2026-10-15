"""
GPU nodes: registration, identity, heartbeats, and the admin state changes
(GPU requirements section 6; FR-GPU-025 to 029, 077, 110 to 117, 136, 168 to 174, 181).

A node moves only along the state model (FR-GPU-028, NODE_TRANSITIONS); any other
change is refused (A-73). Node identity follows architecture section 6.6: registration
issues a one-time enrolment token (24 h), which the agent exchanges once for a long-lived
credential. Only hashes of either are stored.

Stopping work on a node (hard drain, decommission, maintenance, node failure) is done
by the reaper on the tick, through lifecycle.request_stop and lifecycle.release.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import BadRequestError, ConflictError, NotFoundError
from logic.scheduler.claim import utc_now
from logic.scheduler.domain import NODE_TRANSITIONS, AlertKind, DonationMode, DrainMode, JobState, NodeState
from logic.scheduler.requests import largest_reachable_node

log = logging.getLogger("prayog.scheduler")
ENROLMENT_VALIDITY = timedelta(hours=24)
NO_CAPABLE_NODE = "NO_CAPABLE_NODE"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


# ── state changes ──────────────────────────────────────────────────────

def transition(node: orm.Node, to: NodeState, now: datetime, reason: str | None = None) -> None:
    """Move a locked node along the state model, or refuse (FR-GPU-028, A-73)."""
    current = NodeState(node.state)
    if to not in NODE_TRANSITIONS[current]:
        raise ConflictError(f"Node {node.name} is {current}; it cannot become {to}.")
    node.state = to
    node.state_changed_at = now
    node.state_reason = reason


def _lock_node(db: Session, node_id: int) -> orm.Node:
    node = db.scalars(
        select(orm.Node).where(orm.Node.id == node_id).with_for_update().execution_options(populate_existing=True)
    ).first()
    if node is None:
        raise NotFoundError(f"Node {node_id} not found.")
    return node


def _require_reason(reason: str | None) -> str:
    if not reason or not reason.strip():
        raise BadRequestError("A reason is required for this action.")
    return reason.strip()


def _committed(db: Session, work):
    try:
        result = work()
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return result


# ── registration (FR-GPU-025, 110, 181) ────────────────────────────────

@dataclass(frozen=True)
class Registration:
    node: orm.Node
    enrolment_token: str              # shown once; the agent exchanges it for its credential


def _check_sharing(donation_mode: str, teaching_pool: bool) -> None:
    if DonationMode(donation_mode) != DonationMode.SHARED and teaching_pool:
        raise BadRequestError("A teaching-pool node must be shared (FR-GPU-181).")


def register_node(
    db: Session,
    name: str,
    owner_lab_id: int,
    donation_mode: str,
    teaching_pool: bool,
    unit_count: int,
    now: datetime | None = None,
) -> Registration:
    """
    Register a node in PENDING_AGENT with its units. It becomes AVAILABLE only after
    its agent's first heartbeat reports the declared unit count (FR-GPU-110).
    """
    now = now or utc_now()

    def work():
        if db.get(orm.Lab, owner_lab_id) is None:
            raise BadRequestError(f"Unknown owner lab {owner_lab_id}.")
        if donation_mode not in {m.value for m in DonationMode}:
            raise BadRequestError("Donation mode must be reserved or shared.")
        _check_sharing(donation_mode, teaching_pool)
        if unit_count < 1:
            raise BadRequestError("A node needs at least one GPU unit.")
        if db.scalar(select(orm.Node.id).where(orm.Node.name == name)) is not None:
            raise ConflictError(f"A node named {name} is already registered.")

        token = _new_secret()
        node = orm.Node(
            name=name, owner_lab_id=owner_lab_id, donation_mode=donation_mode, teaching_pool=teaching_pool,
            state=NodeState.PENDING_AGENT, unit_count=unit_count, state_changed_at=now,
            enrolment_token_hash=_hash(token), enrolment_expires_at=now + ENROLMENT_VALIDITY,
        )
        db.add(node)
        db.flush()
        db.add_all(orm.GpuUnit(node_id=node.id, unit_index=i) for i in range(unit_count))
        return Registration(node, token)

    return _committed(db, work)


# ── identity (architecture section 6.6) ────────────────────────────────

def enrol(db: Session, enrolment_token: str, now: datetime | None = None) -> tuple[orm.Node, str]:
    """Exchange a valid, unused, unexpired enrolment token for the node's credential."""
    now = now or utc_now()

    def work():
        node = db.scalars(
            select(orm.Node).where(orm.Node.enrolment_token_hash == _hash(enrolment_token)).with_for_update()
        ).first()
        if node is None or node.enrolment_expires_at is None or node.enrolment_expires_at <= now:
            raise BadRequestError("This enrolment token is invalid, used or expired.")
        credential = _new_secret()
        node.credential_hash = _hash(credential)
        node.enrolment_token_hash = None                  # one-time
        node.enrolment_expires_at = None
        return node, credential

    return _committed(db, work)


def authenticate(db: Session, credential: str | None) -> orm.Node | None:
    """The node a credential belongs to, or None if unknown, revoked or decommissioned (FR-GPU-115)."""
    if not credential:
        return None
    node = db.scalars(select(orm.Node).where(orm.Node.credential_hash == _hash(credential))).first()
    if node is None or node.credential_revoked_at is not None or node.state == NodeState.DECOMMISSIONED:
        return None
    return node


# ── heartbeats (FR-GPU-077, 110, 136, 169, 170) ────────────────────────

@dataclass(frozen=True)
class HeartbeatResult:
    state: NodeState
    terminate_allocation_ids: tuple[int, ...]    # containers whose allocation is no longer live (FR-GPU-136)
    became_available: bool


def heartbeat(
    db: Session, node_id: int, unit_count: int, running_allocation_ids: list[int], now: datetime | None = None
) -> HeartbeatResult:
    """
    Record liveness and the reported inventory, and complete the transitions that wait
    for a healthy heartbeat: first heartbeat (PENDING_AGENT), rejoin after DOWN, and an
    admin's return to service. Each needs the registered unit count, and the last two
    need every orphaned container terminated first.
    """
    now = now or utc_now()

    def work():
        node = _lock_node(db, node_id)
        node.last_heartbeat_at = now
        node.reported_unit_count = unit_count
        live = set(db.scalars(
            select(orm.Allocation.id).where(orm.Allocation.node_id == node.id, orm.Allocation.released_at.is_(None))
        ))
        orphans = tuple(sorted(set(running_allocation_ids) - live))
        count_ok = unit_count == node.unit_count
        state = NodeState(node.state)
        became_available = False

        if state == NodeState.PENDING_AGENT:
            if count_ok:
                transition(node, NodeState.AVAILABLE, now)
                clear_alerts(db, node.id, AlertKind.UNIT_COUNT_MISMATCH, now)
                became_available = True
            else:
                raise_alert(db, node.id, AlertKind.UNIT_COUNT_MISMATCH,
                            f"Registered {node.unit_count} units; the agent reports {unit_count}.", now)
        elif state == NodeState.DOWN or (state == NodeState.DRAINING and node.return_requested):
            if not count_ok:
                raise_alert(db, node.id, AlertKind.UNIT_COUNT_CHANGED,
                            f"Registered {node.unit_count} units; the returning agent reports {unit_count}.", now)
            elif not orphans:
                transition(node, NodeState.AVAILABLE, now)
                node.return_requested = False
                node.drain_mode = None
                clear_alerts(db, node.id, AlertKind.NODE_DOWN, now)
                clear_alerts(db, node.id, AlertKind.UNIT_COUNT_CHANGED, now)
                became_available = True
        return HeartbeatResult(NodeState(node.state), orphans, became_available)

    result = _committed(db, work)
    if result.became_available:
        cancel_unsatisfiable_jobs(db, now)                 # a returning node may have fewer units (FR-GPU-117)
    return result


# ── admin actions (FR-GPU-112 to 114, 168 to 172) ──────────────────────

def drain(db: Session, node_id: int, mode: str, reason: str, now: datetime | None = None) -> orm.Node:
    """Take a node out of service for repair (FR-GPU-168). Hard: running jobs get drain_grace (FR-GPU-080)."""
    now = now or utc_now()
    reason = _require_reason(reason)

    def work():
        node = _lock_node(db, node_id)
        transition(node, NodeState.DRAINING, now, reason)
        node.drain_mode = DrainMode(mode)
        node.return_requested = False
        return node

    return _committed(db, work)


def return_to_service(db: Session, node_id: int, now: datetime | None = None) -> orm.Node:
    """
    Ask for a DRAINING node to come back. It becomes AVAILABLE on the next heartbeat that
    reports the registered unit count with no orphaned containers (FR-GPU-169, 170).
    """
    def work():
        node = _lock_node(db, node_id)
        if node.state != NodeState.DRAINING:
            raise ConflictError(f"Node {node.name} is {node.state}; only a DRAINING node can return to service.")
        node.return_requested = True
        return node

    return _committed(db, work)


def decommission(db: Session, node_id: int, mode: str, reason: str, now: datetime | None = None) -> orm.Node:
    """
    Retire a node from AVAILABLE, DRAINING or DOWN (FR-GPU-112, 172). No new placements;
    soft lets running jobs end, hard stops them after drain_grace (FR-GPU-113, 114). It
    becomes DECOMMISSIONED once it holds no live allocation (the reaper, FR-GPU-115).
    """
    now = now or utc_now()
    reason = _require_reason(reason)

    def work():
        node = _lock_node(db, node_id)
        transition(node, NodeState.DECOMMISSIONING, now, reason)
        node.drain_mode = DrainMode(mode)
        node.return_requested = False
        return node

    return _committed(db, work)


def escalate(db: Session, node_id: int, reason: str, now: datetime | None = None) -> orm.Node:
    """DRAINING -> DECOMMISSIONING, keeping the drain mode (FR-GPU-171)."""
    now = now or utc_now()
    reason = _require_reason(reason)

    def work():
        node = _lock_node(db, node_id)
        if node.state != NodeState.DRAINING:
            raise ConflictError(f"Node {node.name} is {node.state}; only a DRAINING node can be escalated.")
        transition(node, NodeState.DECOMMISSIONING, now, reason)
        return node

    return _committed(db, work)


def abort_decommission(db: Session, node_id: int, reason: str, now: datetime | None = None) -> orm.Node:
    """DECOMMISSIONING -> DRAINING (FR-GPU-171)."""
    now = now or utc_now()
    reason = _require_reason(reason)

    def work():
        node = _lock_node(db, node_id)
        transition(node, NodeState.DRAINING, now, reason)
        return node

    return _committed(db, work)


def change_sharing(
    db: Session,
    node_id: int,
    admin_id: int,
    reason: str,
    donation_mode: str | None = None,
    teaching_pool: bool | None = None,
    now: datetime | None = None,
) -> orm.Node:
    """
    Change how a node is shared (US-ADM-09; FR-GPU-111, 181). Only the node row changes:
    running jobs keep their units, and the next pass loads the new settings. A reserved
    node in the teaching pool is refused. The change and its reason are audited.
    """
    now = now or utc_now()
    reason = _require_reason(reason)
    if donation_mode is None and teaching_pool is None:
        raise BadRequestError("Give a donation mode, a teaching-pool flag, or both.")

    def work():
        node = _lock_node(db, node_id)
        if node.state in (NodeState.DECOMMISSIONING, NodeState.DECOMMISSIONED):
            raise ConflictError(f"Node {node.name} is {node.state}; its sharing can no longer change.")
        mode = node.donation_mode if donation_mode is None else donation_mode
        teaching = node.teaching_pool if teaching_pool is None else teaching_pool
        if mode not in {m.value for m in DonationMode}:
            raise BadRequestError("Donation mode must be reserved or shared.")
        _check_sharing(mode, teaching)
        changes = [f"node:{node.id}"]
        if mode != node.donation_mode:
            changes.append(f"donation_mode:{node.donation_mode}->{mode}")
        if teaching != node.teaching_pool:
            changes.append(f"teaching_pool:{str(node.teaching_pool).lower()}->{str(teaching).lower()}")
        node.donation_mode, node.teaching_pool = mode, teaching
        db.add(orm.AuditEvent(
            at=now, actor=str(admin_id), actor_kind="user", operation="node.sharing_change",
            resource_ids=changes, outcome="CHANGED" if len(changes) > 1 else "UNCHANGED", reason=reason[:200],
        ))
        return node

    node = _committed(db, work)
    log.info("node %s sharing set to %s, teaching_pool=%s by admin %s", node_id, node.donation_mode,
             node.teaching_pool, admin_id)
    return node


def confirm_unit_count(db: Session, node_id: int, unit_count: int, now: datetime | None = None) -> orm.Node:
    """
    The admin confirms a returning node's new unit count (FR-GPU-170): units are added, or
    the highest-numbered ones retired (kept for history, never placed on again).
    """
    now = now or utc_now()

    def work():
        node = _lock_node(db, node_id)
        if node.state not in (NodeState.DRAINING, NodeState.DOWN, NodeState.PENDING_AGENT):
            raise ConflictError(f"Node {node.name} is {node.state}; take it out of service first.")
        if unit_count < 1:
            raise BadRequestError("A node needs at least one GPU unit.")
        units = db.scalars(
            select(orm.GpuUnit).where(orm.GpuUnit.node_id == node.id, orm.GpuUnit.retired_at.is_(None))
            .order_by(orm.GpuUnit.unit_index)
        ).all()
        if unit_count < len(units):
            dropped = units[unit_count:]
            if any(u.current_allocation_id is not None for u in dropped):
                raise ConflictError("A unit to be removed still holds an allocation.")
            for unit in dropped:
                unit.retired_at = now
        else:
            top = db.scalar(select(func.max(orm.GpuUnit.unit_index)).where(orm.GpuUnit.node_id == node.id))
            db.add_all(orm.GpuUnit(node_id=node.id, unit_index=top + 1 + i) for i in range(unit_count - len(units)))
        node.unit_count = unit_count
        clear_alerts(db, node.id, AlertKind.UNIT_COUNT_CHANGED, now)
        clear_alerts(db, node.id, AlertKind.UNIT_COUNT_MISMATCH, now)
        return node

    return _committed(db, work)


# ── alerts (FR-GPU-174) ────────────────────────────────────────────────

def raise_alert(db: Session, node_id: int, kind: AlertKind, detail: str, now: datetime) -> None:
    """Open an alert unless one of this kind is already open for the node."""
    open_alert = db.scalar(select(orm.NodeAlert.id).where(
        orm.NodeAlert.node_id == node_id, orm.NodeAlert.kind == kind,
        orm.NodeAlert.cleared_at.is_(None), orm.NodeAlert.acknowledged_at.is_(None),
    ))
    if open_alert is None:
        db.add(orm.NodeAlert(node_id=node_id, kind=kind, detail=detail, raised_at=now))
        log.warning("node alert %s on node %s: %s", kind, node_id, detail)


def clear_alerts(db: Session, node_id: int, kind: AlertKind, now: datetime) -> None:
    """The node left the alerting condition."""
    for alert in db.scalars(select(orm.NodeAlert).where(
        orm.NodeAlert.node_id == node_id, orm.NodeAlert.kind == kind,
        orm.NodeAlert.cleared_at.is_(None), orm.NodeAlert.acknowledged_at.is_(None),
    )):
        alert.cleared_at = now


def acknowledge_alert(db: Session, alert_id: int, reason: str, now: datetime | None = None) -> orm.NodeAlert:
    now = now or utc_now()
    reason = _require_reason(reason)

    def work():
        alert = db.get(orm.NodeAlert, alert_id, with_for_update=True)
        if alert is None:
            raise NotFoundError(f"Alert {alert_id} not found.")
        if alert.cleared_at is not None or alert.acknowledged_at is not None:
            raise ConflictError("This alert is already closed.")
        alert.acknowledged_at, alert.acknowledged_reason = now, reason
        return alert

    return _committed(db, work)


def open_alerts(db: Session) -> list[orm.NodeAlert]:
    return list(db.scalars(
        select(orm.NodeAlert)
        .where(orm.NodeAlert.cleared_at.is_(None), orm.NodeAlert.acknowledged_at.is_(None))
        .order_by(orm.NodeAlert.raised_at)
    ))


# ── jobs that can never run (FR-GPU-117) ───────────────────────────────

def cancel_unsatisfiable_jobs(db: Session, now: datetime | None = None) -> list[int]:
    """
    Cancel QUEUED jobs larger than any node their account could ever use, with reason
    NO_CAPABLE_NODE. Not while any node is DRAINING or DECOMMISSIONING, since a repaired
    node may come back.
    """
    now = now or utc_now()

    def work():
        busy = db.scalar(select(func.count()).select_from(orm.Node).where(
            orm.Node.state.in_((NodeState.DRAINING, NodeState.DECOMMISSIONING))
        ))
        if busy:
            return []
        cancelled = []
        largest: dict[tuple[int, str], int] = {}
        for job in db.scalars(
            select(orm.Job).where(orm.Job.state == JobState.QUEUED).with_for_update(skip_locked=True)
        ):
            key = (job.lab_id, job.account_type)
            if key not in largest:
                largest[key] = largest_reachable_node(db, job.lab_id, job.account_type)
            if job.gpus > largest[key]:
                job.state = JobState.CANCELLED
                job.state_reason = NO_CAPABLE_NODE
                job.cancel_reason = f"No node can ever run {job.gpus} GPUs; the largest now has {largest[key]}."
                cancelled.append(job.id)
        if cancelled:
            log.info("cancelled jobs %s: NO_CAPABLE_NODE", cancelled)    # owner notification: FR-GPU-176
        return cancelled

    return _committed(db, work)


# ── the admin's picture of the pool ────────────────────────────────────

@dataclass(frozen=True)
class NodeSummary:
    node: orm.Node
    units: int
    free_units: int
    open_alerts: int


def node_summaries(db: Session) -> list[NodeSummary]:
    nodes = db.scalars(select(orm.Node).order_by(orm.Node.id)).all()
    rows = db.execute(
        select(orm.GpuUnit.node_id, func.count(), func.count().filter(orm.GpuUnit.current_allocation_id.is_(None)))
        .where(orm.GpuUnit.retired_at.is_(None))
        .group_by(orm.GpuUnit.node_id)
    ).all()
    counts = {node_id: (total, free) for node_id, total, free in rows}
    alerts = dict(db.execute(
        select(orm.NodeAlert.node_id, func.count())
        .where(orm.NodeAlert.cleared_at.is_(None), orm.NodeAlert.acknowledged_at.is_(None))
        .group_by(orm.NodeAlert.node_id)
    ).all())
    return [NodeSummary(n, *counts.get(n.id, (0, 0)), alerts.get(n.id, 0)) for n in nodes]
