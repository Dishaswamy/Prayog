"""Hardware warnings (S2 side): the server's deterministic threshold, lifecycle and the
admin's view (FR-GPU-152, 158, 162-165; SADD ADR-21, ST-07).

The server, not the model, decides when a warning is raised: unconsumed evidence on one
unit from at least ``min_distinct_users_for_warning`` distinct users within
``correlation_window``, and no cooldown after a dismissal. The S2 result is attached
later as advice. A warning never changes node or unit state; freezing and draining are
the admin's separate operations in the GPU subsystem (FR-GPU-152).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, Engine, func, select, text, update

from prayog_steward.config import StewardSettings
from prayog_steward.coordinator.tasks import enqueue
from prayog_steward.db.models import (
    fault_evidence,
    hardware_findings,
    hardware_warnings,
    warning_evidence,
)
from prayog_steward.ports import AllocationHistory, AllocationRecord
from steward_contracts import Context


class Evaluation(StrEnum):
    RAISED = "raised"
    ATTACHED = "attached"
    COOLDOWN = "cooldown"
    BELOW_THRESHOLD = "below_threshold"


class Resolution(StrEnum):
    RESOLVED = "RESOLVED"  # the admin froze the units or drained the node
    DISMISSED = "DISMISSED"  # the evidence does not point at hardware


class WarningNotOpen(Exception):
    pass


def evaluate_unit(
    conn: Connection, unit_id: str, node_id: str, now: datetime, settings: StewardSettings
) -> tuple[Evaluation, uuid.UUID | None]:
    """Run inside the transaction that appended the evidence.

    A per-unit advisory lock serialises concurrent seals on the same unit, so two failures
    committed at the same moment cannot each see only one user and miss the threshold.
    """
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"steward-unit:{unit_id}"},
    )
    open_id = conn.execute(
        select(hardware_warnings.c.warning_id).where(
            hardware_warnings.c.unit_signature == unit_id, hardware_warnings.c.state == "OPEN"
        )
    ).scalar()
    if open_id is not None:
        _attach_unconsumed(conn, open_id, unit_id, since=None)  # FR-GPU-164
        return Evaluation.ATTACHED, open_id

    last_dismissed = conn.execute(
        select(func.max(hardware_warnings.c.closed_at)).where(
            hardware_warnings.c.unit_signature == unit_id,
            hardware_warnings.c.state == "DISMISSED",
        )
    ).scalar()
    if last_dismissed is not None and last_dismissed > now - settings.warning_cooldown:
        return Evaluation.COOLDOWN, None

    since = now - settings.correlation_window
    owners = conn.execute(
        select(func.count(func.distinct(fault_evidence.c.owner_id))).where(
            fault_evidence.c.unit_id == unit_id,
            fault_evidence.c.consumed_at.is_(None),
            fault_evidence.c.at >= since,
        )
    ).scalar_one()
    if owners < settings.min_distinct_users_for_warning:
        return Evaluation.BELOW_THRESHOLD, None

    warning_id = uuid.uuid4()
    conn.execute(
        hardware_warnings.insert().values(
            warning_id=warning_id,
            unit_signature=unit_id,
            node_id=node_id,
            state="OPEN",
            opened_at=now,
        )
    )
    _attach_unconsumed(conn, warning_id, unit_id, since=since)
    enqueue(conn, Context.S2, str(warning_id), now)
    return Evaluation.RAISED, warning_id


def _attach_unconsumed(
    conn: Connection, warning_id: uuid.UUID, unit_id: str, since: datetime | None
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO steward.warning_evidence (warning_id, evidence_id)
            SELECT :w, evidence_id FROM steward.fault_evidence
             WHERE unit_id = :u AND consumed_at IS NULL
               AND (CAST(:since AS timestamptz) IS NULL OR at >= :since)
            ON CONFLICT DO NOTHING
            """
        ),
        {"w": warning_id, "u": unit_id, "since": since},
    )


def close_warning(
    engine: Engine,
    warning_id: uuid.UUID,
    *,
    admin_id: str,
    resolution: Resolution,
    reason: str,
    now: datetime,
) -> None:
    """Only a Platform Admin closes a warning, always with a reason. Closing consumes the
    cited evidence, so the next failure starts a fresh incident (FR-GPU-163, §8.6)."""
    if not reason.strip():
        raise ValueError("a reason is required to close a warning")
    with engine.begin() as conn:
        closed = conn.execute(
            update(hardware_warnings)
            .where(
                hardware_warnings.c.warning_id == warning_id, hardware_warnings.c.state == "OPEN"
            )
            .values(state=resolution.value, closed_at=now, closed_by=admin_id, close_reason=reason)
            .returning(hardware_warnings.c.warning_id)
        ).first()
        if closed is None:
            raise WarningNotOpen(str(warning_id))
        conn.execute(
            text(
                """
                UPDATE steward.fault_evidence SET consumed_at = :now
                 WHERE consumed_at IS NULL AND evidence_id IN (
                       SELECT evidence_id FROM steward.warning_evidence WHERE warning_id = :w)
                """
            ),
            {"now": now, "w": warning_id},
        )


def attach_advice(conn: Connection, warning_id: uuid.UUID, finding: dict[str, Any]) -> bool:
    """Store the S2 result on a still-OPEN warning. Advisory only (ADR-S05)."""
    result = conn.execute(
        update(hardware_warnings)
        .where(hardware_warnings.c.warning_id == warning_id, hardware_warnings.c.state == "OPEN")
        .values(steward_finding=finding)
        .returning(hardware_warnings.c.warning_id)
    )
    return result.first() is not None


# ---------------------------------------------------------------- admin view (FR-GPU-158)


@dataclass(frozen=True)
class EvidenceView:
    """Metadata only: never log bodies, metric values or diagnosis narratives."""

    evidence_id: str
    run_id: str
    job_id: str
    owner_lab: str
    node_id: str
    unit_id: str
    at: datetime
    exit_code: int | None
    failure_category: str
    hardware_verdict: str | None
    hardware_indicators: tuple[str, ...]


@dataclass(frozen=True)
class WarningView:
    warning_id: str
    unit_signature: str
    node_id: str
    state: str
    opened_at: datetime
    closed_at: datetime | None
    closed_by: str | None
    close_reason: str | None
    distinct_labs: int
    evidence_count: int
    steward_finding: dict[str, Any] | None
    evidence: tuple[EvidenceView, ...] = ()
    allocation_history: tuple[AllocationRecord, ...] = field(default_factory=tuple)


def _summary(row: Any, evidence: Sequence[EvidenceView] = ()) -> WarningView:
    return WarningView(
        warning_id=str(row.warning_id),
        unit_signature=row.unit_signature,
        node_id=row.node_id,
        state=row.state,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        closed_by=row.closed_by,
        close_reason=row.close_reason,
        distinct_labs=int(row.distinct_labs or 0),
        evidence_count=int(row.evidence_count or 0),
        steward_finding=row.steward_finding,
        evidence=tuple(evidence),
    )


_SUMMARY = text(
    """
    SELECT w.*, count(e.evidence_id) AS evidence_count,
           count(DISTINCT e.owner_lab) AS distinct_labs
      FROM steward.hardware_warnings w
      LEFT JOIN steward.warning_evidence we ON we.warning_id = w.warning_id
      LEFT JOIN steward.fault_evidence e ON e.evidence_id = we.evidence_id
     WHERE (CAST(:state AS text) IS NULL OR w.state = :state)
       AND (CAST(:wid AS uuid) IS NULL OR w.warning_id = :wid)
     GROUP BY w.warning_id
     ORDER BY w.opened_at DESC
     LIMIT :limit
    """
)


def list_warnings(engine: Engine, state: str | None = None, limit: int = 100) -> list[WarningView]:
    with engine.connect() as conn:
        rows = conn.execute(_SUMMARY, {"state": state, "wid": None, "limit": limit}).all()
    return [_summary(r) for r in rows]


def warning_detail(
    engine: Engine,
    warning_id: uuid.UUID,
    allocations: AllocationHistory,
    settings: StewardSettings,
) -> WarningView | None:
    with engine.connect() as conn:
        row = conn.execute(_SUMMARY, {"state": None, "wid": warning_id, "limit": 1}).first()
        if row is None:
            return None
        ev_rows = conn.execute(
            select(
                fault_evidence.c.evidence_id,
                fault_evidence.c.run_id,
                fault_evidence.c.job_id,
                fault_evidence.c.owner_lab,
                fault_evidence.c.node_id,
                fault_evidence.c.unit_id,
                fault_evidence.c.at,
                fault_evidence.c.exit_code,
                fault_evidence.c.failure_category,
            )
            .join(warning_evidence, warning_evidence.c.evidence_id == fault_evidence.c.evidence_id)
            .where(warning_evidence.c.warning_id == warning_id)
            .order_by(fault_evidence.c.at.desc())
        ).all()
        run_ids = [r.run_id for r in ev_rows]
        findings: dict[str, Any] = {}
        if run_ids:
            for f in conn.execute(
                select(hardware_findings)
                .where(hardware_findings.c.run_id.in_(run_ids))
                .order_by(hardware_findings.c.created_at)
            ).all():
                findings[f.run_id] = f  # latest wins
    evidence = [
        EvidenceView(
            evidence_id=str(r.evidence_id),
            run_id=r.run_id,
            job_id=r.job_id,
            owner_lab=r.owner_lab,
            node_id=r.node_id,
            unit_id=r.unit_id,
            at=r.at,
            exit_code=r.exit_code,
            failure_category=r.failure_category,
            hardware_verdict=findings[r.run_id].verdict if r.run_id in findings else None,
            hardware_indicators=tuple(findings[r.run_id].indicators)
            if r.run_id in findings
            else (),
        )
        for r in ev_rows
    ]
    view = _summary(row, evidence)
    history = allocations.for_units(
        [view.unit_signature], since=view.opened_at - settings.correlation_window
    )
    return WarningView(**{**view.__dict__, "allocation_history": tuple(history)})
