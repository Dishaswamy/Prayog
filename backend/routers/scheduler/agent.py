from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session, sessionmaker

from database import get_db
from logic.scheduler.config import SchedulerConfig
from logic.scheduler.dispatch import acknowledge, allocations_for, report
from logic.scheduler.nodes import authenticate, enrol, heartbeat
from logic.scheduler.passes import Trigger, run_pass
from models import Node
from routers.scheduler.jobs import get_pass_sessions, get_scheduler_config
from schemas.scheduler.agent import (
    AckOut,
    AllocationOut,
    EnrolIn,
    EnrolOut,
    HeartbeatIn,
    HeartbeatOut,
    ReportIn,
    ReportOut,
)

# The node agent's side of the API (architecture section 7.5). Agents pull; nothing is pushed to them.
router = APIRouter(prefix="/agent", tags=["node agent"])


# Every call after enrolment carries the node credential; it only ever reaches its own node's data.
def current_node(
    x_node_credential: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Node:
    node = authenticate(db, x_node_credential)
    if node is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown or revoked node credential")
    return node


# Exchange the one-time enrolment token from registration for the node credential.
@router.post("/enrol", response_model=EnrolOut)
def enrol_node(body: EnrolIn, db: Session = Depends(get_db)):
    node, credential = enrol(db, body.enrolment_token)
    return EnrolOut(node_id=node.id, credential=credential)


# Liveness every heartbeat_interval, with the unit inventory and the containers the agent runs.
@router.post("/heartbeat", response_model=HeartbeatOut)
def node_heartbeat(
    body: HeartbeatIn,
    background: BackgroundTasks,
    node: Node = Depends(current_node),
    db: Session = Depends(get_db),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    result = heartbeat(db, node.id, body.unit_count, body.running_allocation_ids)
    if result.became_available:
        background.add_task(run_pass, pass_sessions, Trigger.NODE_STATE, cfg=cfg)
    return HeartbeatOut(state=result.state, terminate_allocation_ids=list(result.terminate_allocation_ids))


# What this node should be running, or stopping.
@router.get("/allocations", response_model=list[AllocationOut])
def my_allocations(node: Node = Depends(current_node), db: Session = Depends(get_db)):
    return [AllocationOut(**{**vars(o), "unit_indexes": list(o.unit_indexes)}) for o in allocations_for(db, node.id)]


# Confirm the start before any side effect; a 409 means the job was cancelled or requeued: do not start.
@router.post("/allocations/{allocation_id}/acknowledge", response_model=AckOut)
def acknowledge_allocation(allocation_id: int, node: Node = Depends(current_node), db: Session = Depends(get_db)):
    return AckOut(started=acknowledge(db, node.id, allocation_id))


# The run ended and the workspace is unmounted: release its units.
@router.post("/allocations/{allocation_id}/report", response_model=ReportOut)
def report_allocation(
    allocation_id: int,
    body: ReportIn,
    background: BackgroundTasks,
    node: Node = Depends(current_node),
    db: Session = Depends(get_db),
    cfg: SchedulerConfig = Depends(get_scheduler_config),
    pass_sessions: sessionmaker = Depends(get_pass_sessions),
):
    result = report(db, node.id, allocation_id, body.seq, body.outcome, body.exit_code, cfg=cfg)
    if result is None:
        return ReportOut(applied=False, job_state=None)
    background.add_task(run_pass, pass_sessions, Trigger.RUN_END, cfg=cfg)            # FR-GPU-121
    return ReportOut(applied=True, job_state=result.job_state)
