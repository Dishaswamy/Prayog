"""Steward task queue: a transactional outbox in ``steward.steward_tasks`` (SADD §8.3).

Tasks are enqueued inside the platform's run-seal transaction, so a sealed failure is
never lost. Workers claim with ``FOR UPDATE SKIP LOCKED`` and take a lease. A worker that
dies mid-task leaves the lease to expire, and the task is claimed again. Correct for any
number of workers. The model call happens outside any transaction.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, text
from sqlalchemy.dialects.postgresql import insert

from prayog_steward.db.models import steward_tasks
from steward_contracts import Context


@dataclass(frozen=True)
class Task:
    task_id: uuid.UUID
    context: Context
    subject_id: str
    attempts: int  # including the current one


def enqueue(conn: Connection, context: Context, subject_id: str, now: datetime) -> bool:
    """Idempotent: a second enqueue for the same (context, subject) is a no-op."""
    result = conn.execute(
        insert(steward_tasks)
        .values(
            task_id=uuid.uuid4(),
            context=context.value,
            subject_id=subject_id,
            state="PENDING",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["context", "subject_id"])
        .returning(steward_tasks.c.task_id)
    )
    return result.first() is not None


_CLAIM = text(
    """
    UPDATE steward.steward_tasks t
       SET state = 'RUNNING', attempts = t.attempts + 1,
           next_attempt_at = :lease_until, updated_at = :now
     WHERE t.task_id = (
           SELECT task_id FROM steward.steward_tasks
            WHERE state IN ('PENDING', 'RUNNING') AND next_attempt_at <= :now
            ORDER BY next_attempt_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
    RETURNING t.task_id, t.context, t.subject_id, t.attempts
    """
)


def claim(engine: Engine, now: datetime, lease: timedelta) -> Task | None:
    with engine.begin() as conn:
        row = conn.execute(_CLAIM, {"now": now, "lease_until": now + lease}).first()
    if row is None:
        return None
    return Task(row.task_id, Context(row.context), row.subject_id, row.attempts)


def complete(engine: Engine, task: Task, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE steward.steward_tasks SET state = 'DONE', updated_at = :now,"
                " last_error = NULL WHERE task_id = :id"
            ),
            {"now": now, "id": task.task_id},
        )


def backoff(attempts: int, base: timedelta) -> timedelta:
    """Exponential backoff with full jitter, capped at one hour."""
    ceiling = min(base * (2 ** max(attempts - 1, 0)), timedelta(hours=1))
    return timedelta(seconds=random.uniform(0.5, 1.0) * ceiling.total_seconds())


def retry_or_give_up(
    engine: Engine,
    task: Task,
    now: datetime,
    error: str,
    *,
    max_attempts: int,
    base: timedelta,
) -> bool:
    """Schedule a retry. Returns True if the task has used up its attempts (GAVE_UP)."""
    gave_up = task.attempts >= max_attempts
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE steward.steward_tasks SET state = :state, next_attempt_at = :next,"
                " last_error = :err, updated_at = :now WHERE task_id = :id"
            ),
            {
                "state": "GAVE_UP" if gave_up else "PENDING",
                "next": now if gave_up else now + backoff(task.attempts, base),
                "err": error[:500],
                "now": now,
                "id": task.task_id,
            },
        )
    return gave_up


def queue_depth(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT state, count(*) AS n FROM steward.steward_tasks GROUP BY state")
        ).all()
    return {r.state: int(r.n) for r in rows}
