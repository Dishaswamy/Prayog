"""Small builders so each test reads like the scenario it checks."""

from datetime import datetime, timedelta

from scheduler.domain import (
    AccountType,
    DonationMode,
    JobView,
    MaintenanceWindow,
    NodeState,
    NodeView,
    UnitView,
)

NOW = datetime(2026, 10, 13, 12, 0, 0)

NLP, ROBOTICS, VISION = 1, 2, 3          # lab ids
CENTRAL = 99                             # institute-owned teaching hardware
COURSE_GROUP = 50                        # a course sponsorship group


def node(
    node_id: int = 1,
    owner: int = NLP,
    units: int = 4,
    free: int | None = None,
    shared: bool = True,
    teaching: bool = False,
    state: NodeState = NodeState.AVAILABLE,
    maintenance: tuple[MaintenanceWindow, ...] = (),
) -> NodeView:
    free = units if free is None else free
    return NodeView(
        node_id=node_id,
        owner_lab_id=owner,
        donation_mode=DonationMode.SHARED if shared else DonationMode.RESERVED,
        teaching_pool=teaching,
        state=state,
        units=tuple(
            UnitView(unit_id=node_id * 100 + i, unit_index=i, free=i < free, maintenance=maintenance)
            for i in range(units)
        ),
    )


def job(
    job_id: int = 1,
    lab: int = NLP,
    gpus: int = 2,
    walltime_h: float = 4,
    waited_min: float = 0,
    course: bool = False,
) -> JobView:
    return JobView(
        job_id=job_id,
        lab_id=lab,
        account_type=AccountType.COURSE if course else AccountType.RESEARCH,
        gpus=gpus,
        walltime_minutes=int(walltime_h * 60),
        submit_time=NOW - timedelta(minutes=waited_min),
    )


def window(start_in_h: float, length_h: float = 2) -> MaintenanceWindow:
    start = NOW + timedelta(hours=start_in_h)
    return MaintenanceWindow(starts_at=start, ends_at=start + timedelta(hours=length_h))
