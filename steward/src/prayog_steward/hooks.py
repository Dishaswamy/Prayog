"""Entry point the platform calls when a run is sealed (SADD §8.3, ADR-S07).

Call ``on_run_sealed`` **inside the platform's run-sealing transaction**, passing that
transaction's connection. The failure event, the fault-evidence rows and the Steward task
then commit atomically with the sealed run, or not at all. Nothing here calls the model
or blocks on the Steward (NFR-GPU-055); the Worker picks the task up afterwards.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert

from prayog_steward.clock import Clock
from prayog_steward.config import StewardSettings
from prayog_steward.coordinator.tasks import enqueue
from prayog_steward.db.models import failure_events, fault_evidence
from prayog_steward.hardware import Evaluation, evaluate_unit
from prayog_steward.ports import RunEnd, RunView
from prayog_steward.rag.sanitiser import extract_error_facts
from steward_contracts import Context

NODE_FAILURE = "NODE_FAILURE"


@dataclass(frozen=True)
class SealOutcome:
    diagnosis_enqueued: bool = False
    evidence_rows: int = 0
    warnings: dict[str, Evaluation] = field(default_factory=dict)  # unit -> evaluation


def on_run_sealed(
    conn: Connection,
    run: RunView,
    *,
    settings: StewardSettings,
    clock: Clock,
    log_tail: str | None = None,
) -> SealOutcome:
    """Record what the Steward needs about a terminal run.

    - ``FAILED``: failure event, one evidence row per unit, S1 task (FR-GPU-146, 159).
    - ``INTERRUPTED`` with reason ``NODE_FAILURE``: evidence only, no diagnosis (FR-GPU-147).
    - Anything else (``SUCCEEDED``, ``CANCELLED``, ``WALLTIME_EXCEEDED``, other
      interruptions): nothing (FR-GPU-147, FR-STW-007).

    ``log_tail`` is optional. If given, only its exception class and library frame are
    kept, as the evidence row's failure signature (ADR-S05). The text itself is not stored.
    Idempotent: sealing the same run twice records nothing new.
    """
    failed = run.end is RunEnd.FAILED
    node_failure = run.end is RunEnd.INTERRUPTED and run.interrupt_reason == NODE_FAILURE
    if not (failed or node_failure):
        return SealOutcome()

    now = clock.now()
    category = run.failure_category or (NODE_FAILURE if node_failure else "UNKNOWN")
    enqueued = False
    if failed:
        conn.execute(
            insert(failure_events)
            .values(
                event_id=uuid.uuid4(),
                run_id=run.run_id,
                job_id=run.job_id,
                node_id=run.node_id,
                unit_ids=list(run.unit_ids),
                exit_code=run.exit_code,
                failure_category=category,
                log_refs=list(run.log_refs),
                at=run.ended_at,
            )
            .on_conflict_do_nothing(index_elements=["run_id"])
        )
        enqueued = enqueue(conn, Context.S1, run.run_id, now)

    signature = (
        extract_error_facts(log_tail, run.job_spec.env_pins).failure_signature if log_tail else None
    )
    inserted = 0
    for unit_id in run.unit_ids:
        result = conn.execute(
            insert(fault_evidence)
            .values(
                evidence_id=uuid.uuid4(),
                run_id=run.run_id,
                job_id=run.job_id,
                node_id=run.node_id,
                unit_id=unit_id,
                owner_lab=run.owner_lab,
                owner_id=run.owner_id,
                exit_code=run.exit_code,
                failure_category=category,
                failure_signature=signature,
                at=run.ended_at,
            )
            .on_conflict_do_nothing(index_elements=["run_id", "unit_id"])
            .returning(fault_evidence.c.evidence_id)
        )
        inserted += int(result.first() is not None)

    evaluations: dict[str, Evaluation] = {}
    if inserted:
        for unit_id in run.unit_ids:
            evaluations[unit_id], _ = evaluate_unit(conn, unit_id, run.node_id, now, settings)
    return SealOutcome(enqueued, inserted, evaluations)
