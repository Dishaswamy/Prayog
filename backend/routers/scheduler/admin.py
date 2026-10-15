from fastapi import APIRouter, BackgroundTasks, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from database import get_db
from logic.dependencies import require_roles
from logic.scheduler import maintenance
from logic.scheduler import nodes as node_ops
from logic.scheduler.cancel import admin_cancel_job
from logic.scheduler.config import SchedulerConfig
from logic.scheduler.passes import Trigger, run_pass
from models import User
from routers.scheduler.jobs import get_pass_sessions, get_scheduler_config
from schemas.scheduler.jobs import CancelOut
from schemas.scheduler.admin import (
    AlertOut,
    BookingOut,
    CollisionOut,
    DrainIn,
    MaintenanceBookIn,
    MaintenanceMoveIn,
    MaintenancePreviewIn,
    NodeOut,
    NodeRegister,
    NodeRegistered,
    PreviewOut,
    Reason,
    UnitCountIn,
)

# Platform Admin operations on the GPU pool (FR-GPU-175). Every route needs the ADMIN role.
router = APIRouter(prefix="/admin", tags=["admin: GPU pool"])
admin_only = require_roles("ADMIN")


def _node_out(db: Session, node_id: int) -> NodeOut:
    s = next(s for s in node_ops.node_summaries(db) if s.node.id == node_id)
    n = s.node
    return NodeOut(
        id=n.id, name=n.name, owner_lab_id=n.owner_lab_id, donation_mode=n.donation_mode,
        teaching_pool=n.teaching_pool, state=n.state, state_reason=n.state_reason, drain_mode=n.drain_mode,
        return_requested=n.return_requested, unit_count=n.unit_count, free_units=s.free_units,
        reported_unit_count=n.reported_unit_count, last_heartbeat_at=n.last_heartbeat_at, open_alerts=s.open_alerts,
    )


def _collisions(p: maintenance.Preview) -> list[CollisionOut]:
    return [CollisionOut(**vars(c)) for c in p.collisions]


# ── nodes ──────────────────────────────────────────────────────────────

# Register a node; it stays PENDING_AGENT until its agent's first heartbeat reports the declared units.
@router.post("/nodes", response_model=NodeRegistered, status_code=status.HTTP_201_CREATED)
def register(body: NodeRegister, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    reg = node_ops.register_node(db, **body.model_dump())
    return NodeRegistered(node=_node_out(db, reg.node.id), enrolment_token=reg.enrolment_token)


# The pool at a glance: every node, its state, units and open alerts.
@router.get("/nodes", response_model=list[NodeOut])
def list_nodes(db: Session = Depends(get_db), _: User = Depends(admin_only)):
    return [_node_out(db, s.node.id) for s in node_ops.node_summaries(db)]


# Take a node out of service: soft lets running jobs finish, hard stops them after drain_grace.
@router.post("/nodes/{node_id}/drain", response_model=NodeOut)
def drain(node_id: int, body: DrainIn, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    node_ops.drain(db, node_id, body.mode, body.reason)
    return _node_out(db, node_id)


# Bring a drained node back; it becomes AVAILABLE on its next healthy heartbeat.
@router.post("/nodes/{node_id}/return", response_model=NodeOut)
def return_to_service(node_id: int, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    node_ops.return_to_service(db, node_id)
    return _node_out(db, node_id)


# Retire a node permanently (soft or hard). Its records are kept.
@router.post("/nodes/{node_id}/decommission", response_model=NodeOut)
def decommission(node_id: int, body: DrainIn, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    node_ops.decommission(db, node_id, body.mode, body.reason)
    return _node_out(db, node_id)


# DRAINING -> DECOMMISSIONING.
@router.post("/nodes/{node_id}/escalate", response_model=NodeOut)
def escalate(node_id: int, body: Reason, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    node_ops.escalate(db, node_id, body.reason)
    return _node_out(db, node_id)


# DECOMMISSIONING -> DRAINING: abort a decommission.
@router.post("/nodes/{node_id}/abort-decommission", response_model=NodeOut)
def abort_decommission(node_id: int, body: Reason, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    node_ops.abort_decommission(db, node_id, body.reason)
    return _node_out(db, node_id)


# Confirm a returning node's changed unit count.
@router.post("/nodes/{node_id}/unit-count", response_model=NodeOut)
def confirm_unit_count(node_id: int, body: UnitCountIn, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    node_ops.confirm_unit_count(db, node_id, body.unit_count)
    return _node_out(db, node_id)


# ── jobs ───────────────────────────────────────────────────────────────

# Cancel any user's job. The reason is mandatory and audited with the admin and the time.
@router.post("/jobs/{job_id}/cancel", response_model=CancelOut)
def cancel_any_job(
    job_id: int,
    body: Reason,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_only),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    result = admin_cancel_job(db, job_id, admin.id, body.reason, cfg=cfg)
    background.add_task(run_pass, pass_sessions, Trigger.CANCEL, cfg=cfg)
    return CancelOut.from_result(result)


# ── alerts ─────────────────────────────────────────────────────────────

@router.get("/alerts", response_model=list[AlertOut])
def alerts(db: Session = Depends(get_db), _: User = Depends(admin_only)):
    return node_ops.open_alerts(db)


@router.post("/alerts/{alert_id}/acknowledge", status_code=status.HTTP_204_NO_CONTENT)
def acknowledge_alert(alert_id: int, body: Reason, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    node_ops.acknowledge_alert(db, alert_id, body.reason)


# ── maintenance ────────────────────────────────────────────────────────

# Clear-by time of the units and the runs a window starting then would stop. Changes nothing.
@router.post("/maintenance/preview", response_model=PreviewOut)
def preview_maintenance(
    body: MaintenancePreviewIn,
    db: Session = Depends(get_db),
    _: User = Depends(admin_only),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
):
    p = maintenance.preview(db, body.unit_ids, body.starts_at, cfg=cfg)
    return PreviewOut(clear_by=p.clear_by, collisions=_collisions(p))


@router.get("/maintenance", response_model=list[BookingOut])
def list_maintenance(db: Session = Depends(get_db), _: User = Depends(admin_only)):
    return [BookingOut(**{**vars(b), "unit_ids": list(b.unit_ids)}) for b in maintenance.bookings(db)]


# Book a window. If it would stop running jobs, it is refused (409, with the collision set) unless confirm=true.
@router.post("/maintenance", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
def book_maintenance(
    body: MaintenanceBookIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(admin_only),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    try:
        booking_id, p = maintenance.book(db, body.unit_ids, body.starts_at, body.ends_at, body.reason,
                                         confirm=body.confirm, cfg=cfg)
    except maintenance.CollisionNotConfirmed as exc:
        return _collision_response(exc)
    background.add_task(run_pass, pass_sessions, Trigger.MAINTENANCE, cfg=cfg)
    return BookingOut(booking_id=booking_id, unit_ids=sorted(set(body.unit_ids)), starts_at=body.starts_at,
                      ends_at=body.ends_at, reason=body.reason, collisions=_collisions(p))


# Move or narrow a booking; checked against the collision set again.
@router.patch("/maintenance/{booking_id}", response_model=PreviewOut)
def move_maintenance(
    booking_id: str,
    body: MaintenanceMoveIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(admin_only),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    try:
        p = maintenance.move(db, booking_id, body.starts_at, body.ends_at, confirm=body.confirm, cfg=cfg)
    except maintenance.CollisionNotConfirmed as exc:
        return _collision_response(exc)
    background.add_task(run_pass, pass_sessions, Trigger.MAINTENANCE, cfg=cfg)
    return PreviewOut(clear_by=p.clear_by, collisions=_collisions(p))


# Cancel a booking: eligibility comes back at once.
@router.delete("/maintenance/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_maintenance(
    booking_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(admin_only),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    maintenance.cancel(db, booking_id)
    background.add_task(run_pass, pass_sessions, Trigger.MAINTENANCE, cfg=cfg)


def _collision_response(exc: maintenance.CollisionNotConfirmed) -> JSONResponse:
    preview = PreviewOut(clear_by=exc.preview.clear_by, collisions=_collisions(exc.preview))
    return JSONResponse(status_code=409, content={
        "detail": str(exc), "code": exc.code, **preview.model_dump(mode="json"),
    })

