"""
Investigating a suspected hardware fault (US-ADM-05; FR-GPU-158, 163; A-29, A-36).

The Steward raises a hardware warning when failures on one GPU unit cluster across
users, and attaches its finding as advice. It never changes node or unit state
(FR-GPU-152). This module is the admin's side: it shows the warning with the GPU
subsystem's current view of the unit, and carries out the admin's decision:

    freeze    the unit gets a maintenance window starting now (no new placements on it)
    drain     the whole node is taken out of service for repair (soft or hard)
    dismiss   the evidence does not point at hardware; nothing changes

Each decision needs a reason. Freeze and drain close the warning as RESOLVED, dismiss
as DISMISSED; closing consumes the cited evidence, so the next failure starts a fresh
incident (FR-GPU-163). The decision is written to the audit log.

Everything shown is metadata: run and job IDs, labs, exit codes, failure categories,
allocation history. Never logs, metrics or diagnosis text (A-29).
"""

import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

import models as orm
from logic.errors import AppError, BadRequestError, ConflictError, NotFoundError
from logic.scheduler import maintenance
from logic.scheduler import nodes as node_ops
from logic.scheduler.claim import utc_now
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig

log = logging.getLogger("prayog.scheduler")
_UNIT = re.compile(r"^(?P<node>.+)/gpu(?P<index>\d+)$")       # steward_bridge.unit_name()


class Action(StrEnum):
    FREEZE = "freeze"
    DRAIN = "drain"
    DISMISS = "dismiss"


class StewardUnavailable(AppError):
    status_code = 503
    code = "STEWARD_UNAVAILABLE"


class WarningPort(Protocol):
    """What this module needs from the Steward's hardware warnings."""

    def detail(self, warning_id: uuid.UUID) -> dict[str, Any] | None: ...

    def close(self, warning_id: uuid.UUID, admin_id: str, resolution: str, reason: str, now: datetime) -> None: ...


class StewardWarnings:
    """The running Steward (prayog_steward.hardware)."""

    def __init__(self) -> None:
        from integration.steward_bridge import get_steward

        if os.getenv("STEWARD_ENABLED", "true").strip().lower() in ("0", "false", "no"):
            raise StewardUnavailable("The Steward is disabled, so there are no hardware warnings.")
        try:
            self.steward = get_steward()
        except Exception as exc:  # noqa: BLE001 - a missing Steward is a 503, not a crash
            raise StewardUnavailable("The Steward is not running, so there are no hardware warnings.") from exc

    def detail(self, warning_id: uuid.UUID) -> dict[str, Any] | None:
        from prayog_steward.hardware import warning_detail

        view = warning_detail(self.steward.engine, warning_id, self.steward.ports.allocations, self.steward.settings)
        return None if view is None else _plain(view)

    def close(self, warning_id: uuid.UUID, admin_id: str, resolution: str, reason: str, now: datetime) -> None:
        from prayog_steward.hardware import Resolution, WarningNotOpen, close_warning

        try:
            close_warning(self.steward.engine, warning_id, admin_id=admin_id, resolution=Resolution(resolution),
                          reason=reason, now=now)
        except WarningNotOpen as exc:
            raise ConflictError("This warning is already closed.") from exc


def _plain(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


# ── the investigation view ─────────────────────────────────────────────

@dataclass(frozen=True)
class UnitState:
    node_id: int
    node_name: str
    node_state: str
    unit_id: int
    unit_index: int
    allocated: bool
    retired: bool
    maintenance_until: datetime | None


def unit_state(db: Session, unit_signature: str, now: datetime) -> UnitState | None:
    """The GPU subsystem's current view of the unit a warning names, or None if it no longer exists."""
    m = _UNIT.match(unit_signature or "")
    if not m:
        return None
    row = db.execute(
        select(orm.Node, orm.GpuUnit)
        .join(orm.GpuUnit, orm.GpuUnit.node_id == orm.Node.id)
        .where(orm.Node.name == m["node"], orm.GpuUnit.unit_index == int(m["index"]))
    ).first()
    if row is None:
        return None
    node, unit = row
    until = db.scalar(
        select(orm.MaintenanceWindow.ends_at)
        .where(orm.MaintenanceWindow.unit_id == unit.id, orm.MaintenanceWindow.starts_at <= now,
               orm.MaintenanceWindow.ends_at > now)
        .order_by(orm.MaintenanceWindow.ends_at.desc())
    )
    return UnitState(
        node_id=node.id, node_name=node.name, node_state=node.state, unit_id=unit.id, unit_index=unit.unit_index,
        allocated=unit.current_allocation_id is not None, retired=unit.retired_at is not None,
        maintenance_until=until,
    )


def investigate(db: Session, warnings: WarningPort, warning_id: uuid.UUID, now: datetime | None = None) -> dict:
    """The warning (cited runs, unit history, the Steward's finding) with the unit's current state."""
    now = now or utc_now()
    warning = warnings.detail(warning_id)
    if warning is None:
        raise NotFoundError("Hardware warning not found.")
    state = unit_state(db, warning["unit_signature"], now)
    return {"warning": warning, "unit": None if state is None else asdict(state)}


# ── the decision ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    warning_id: str
    action: Action
    resolution: str                       # RESOLVED or DISMISSED
    node_id: int | None
    unit_id: int | None
    booking_id: str | None                # the freeze's maintenance booking


def resolve(
    db: Session,
    warnings: WarningPort,
    warning_id: uuid.UUID,
    admin_id: int,
    action: Action,
    reason: str,
    freeze_until: datetime | None = None,
    drain_mode: str = "soft",
    confirm: bool = False,
    now: datetime | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> Decision:
    """Freeze the unit, drain its node, or dismiss the warning; then close the warning."""
    now = now or utc_now()
    action = Action(action)
    if not reason or not reason.strip():
        raise BadRequestError("A reason is required to resolve a hardware warning.")
    reason = reason.strip()

    warning = warnings.detail(warning_id)
    if warning is None:
        raise NotFoundError("Hardware warning not found.")
    if warning["state"] != "OPEN":
        raise ConflictError(f"This warning is already {warning['state']}.")
    state = unit_state(db, warning["unit_signature"], now)
    if action != Action.DISMISS and state is None:
        raise ConflictError("The unit this warning names no longer exists; dismiss the warning instead.")

    booking_id = None
    if action == Action.FREEZE:
        if freeze_until is None:
            raise BadRequestError("Say until when the unit stays frozen.")
        # Refused with the collision set (409) when a running job would be stopped and confirm is false.
        booking_id, _ = maintenance.book(db, [state.unit_id], now, freeze_until,
                                         f"Hardware warning {warning_id}: {reason}"[:255],
                                         confirm=confirm, now=now, cfg=cfg)
    elif action == Action.DRAIN:
        node_ops.drain(db, state.node_id, drain_mode, f"Hardware warning: {reason}"[:255], now=now)

    resolution = "DISMISSED" if action == Action.DISMISS else "RESOLVED"
    warnings.close(warning_id, str(admin_id), resolution, reason, now)

    db.add(orm.AuditEvent(
        at=now, actor=str(admin_id), actor_kind="user", operation=f"hardware_warning.{action.value}",
        resource_ids=[f"warning:{warning_id}", f"unit:{warning['unit_signature']}"[:200]]
        + ([f"booking:{booking_id}"] if booking_id else []),
        outcome=resolution, reason=reason[:200],
    ))
    db.commit()
    log.info("hardware warning %s: %s by admin %s", warning_id, action.value, admin_id)
    return Decision(str(warning_id), action, resolution, state and state.node_id, state and state.unit_id, booking_id)
