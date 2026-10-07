"""Table definitions used by queries. The DDL of record is the Alembic migrations, which
also install row-level security and append-only triggers that SQLAlchemy cannot express.
``tests/integration/test_schema.py`` checks the two stay in step.

Identifiers owned by other subsystems (runs, jobs, users, nodes, units, attempts) are
opaque ``text``. Our own identifiers are UUIDs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    REAL,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

STEWARD = "steward"
PRIVATE = "steward_private"

metadata = MetaData()


def _ts(name: str, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


def _text(name: str, nullable: bool = False, **kw: Any) -> Column[str]:
    return Column(name, Text, nullable=nullable, **kw)


# ------------------------------------------------------------------ steward (S1, S2)

steward_tasks = Table(
    "steward_tasks",
    metadata,
    Column("task_id", Uuid, primary_key=True),
    _text("context"),  # S1 | S2
    _text("subject_id"),  # run_id for S1, warning_id for S2
    _text("state"),  # PENDING | RUNNING | DONE | GAVE_UP
    Column("attempts", Integer, nullable=False),
    _ts("next_attempt_at"),
    _text("last_error", nullable=True),
    _ts("created_at"),
    _ts("updated_at"),
    schema=STEWARD,
)

failure_events = Table(
    "failure_events",
    metadata,
    Column("event_id", Uuid, primary_key=True),
    _text("run_id", unique=True),
    _text("job_id"),
    _text("node_id"),
    Column("unit_ids", ARRAY(Text), nullable=False),
    Column("exit_code", Integer),
    _text("failure_category"),
    Column("log_refs", ARRAY(Text), nullable=False),
    _ts("at"),
    schema=STEWARD,
)

fault_evidence = Table(
    "fault_evidence",
    metadata,
    Column("evidence_id", Uuid, primary_key=True),
    _text("run_id"),
    _text("job_id"),
    _text("node_id"),
    _text("unit_id"),
    _text("owner_lab"),
    # Needed to count distinct users (FR-GPU-162). An identifier, never shown to S2.
    _text("owner_id"),
    Column("exit_code", Integer),
    _text("failure_category"),
    _text("failure_signature", nullable=True),  # ADR-S05, pending CR-S1
    _ts("at"),
    _ts("consumed_at", nullable=True),
    schema=STEWARD,
)

diagnoses = Table(
    "diagnoses",
    metadata,
    Column("diagnosis_id", Uuid, primary_key=True),
    _text("run_id"),
    _text("project_id"),
    _text("owner_id"),
    _text("outcome"),  # ok | refused | malformed | unavailable
    _text("classification", nullable=True),
    Column("claims", JSONB, nullable=False),
    Column("matching_earlier_failures", ARRAY(Text), nullable=False),
    Column("stripped_count", Integer, nullable=False),
    _text("agent_id"),
    _text("agent_version"),
    _text("model", nullable=True),
    _ts("created_at"),
    schema=STEWARD,
)

hardware_findings = Table(
    "hardware_findings",
    metadata,
    Column("finding_id", Uuid, primary_key=True),
    Column("diagnosis_id", Uuid, ForeignKey(f"{STEWARD}.diagnoses.diagnosis_id"), nullable=False),
    _text("run_id"),
    _text("verdict"),
    Column("indicators", ARRAY(Text), nullable=False),
    _ts("created_at"),
    schema=STEWARD,
)

proposals = Table(
    "proposals",
    metadata,
    Column("proposal_id", Uuid, primary_key=True),
    Column("diagnosis_id", Uuid, ForeignKey(f"{STEWARD}.diagnoses.diagnosis_id"), nullable=False),
    _text("run_id"),
    _text("project_id"),
    _text("owner_id"),
    Column("changes", JSONB, nullable=False),  # the model's JobChange list, citations kept
    Column("job_spec", JSONB, nullable=False),  # the exact resulting spec (FR-GPU-187)
    Column("requested_gpu_hours", Numeric(10, 2), nullable=False),
    _text("state"),  # PROPOSED | APPROVED | REJECTED
    _text("decided_by", nullable=True),
    _ts("decided_at", nullable=True),
    _text("submitted_job_id", nullable=True),
    _text("submission_refusal", nullable=True),
    _ts("created_at"),
    schema=STEWARD,
)

hardware_warnings = Table(
    "hardware_warnings",
    metadata,
    Column("warning_id", Uuid, primary_key=True),
    _text("unit_signature"),
    _text("node_id"),
    _text("state"),  # OPEN | RESOLVED | DISMISSED
    _ts("opened_at"),
    _ts("closed_at", nullable=True),
    _text("closed_by", nullable=True),
    _text("close_reason", nullable=True),
    Column("steward_finding", JSONB),  # advisory S2 result (ADR-S05)
    schema=STEWARD,
)

warning_evidence = Table(
    "warning_evidence",
    metadata,
    Column(
        "warning_id",
        Uuid,
        ForeignKey(f"{STEWARD}.hardware_warnings.warning_id"),
        primary_key=True,
    ),
    Column(
        "evidence_id",
        Uuid,
        ForeignKey(f"{STEWARD}.fault_evidence.evidence_id"),
        primary_key=True,
    ),
    schema=STEWARD,
)

# ------------------------------------------------------------------ steward_private (S3, S4)

steward_links = Table(  # not under RLS; see migration 0001
    "steward_links",
    metadata,
    Column("token_hash", LargeBinary, primary_key=True),
    _text("attempt_id"),
    _text("user_id"),
    _text("session_id"),
    _ts("created_at"),
    _ts("expires_at"),
    _ts("consumed_at", nullable=True),
    schema=PRIVATE,
)

s3_sessions = Table(
    "s3_sessions",
    metadata,
    Column("session_hash", LargeBinary, primary_key=True),
    _text("user_id"),
    _text("attempt_id"),
    _ts("created_at"),
    _ts("expires_at"),
    _ts("revoked_at", nullable=True),
    schema=PRIVATE,
)

rag_chunks = Table(
    "rag_chunks",
    metadata,
    Column("chunk_id", Uuid, primary_key=True),
    _text("user_id"),
    _text("attempt_id"),
    _text("source_ref"),
    Column("seq", Integer, nullable=False),
    _text("kind"),
    _text("body"),  # sanitised (ADR-S03)
    _text("embedder"),
    Column("embedding", ARRAY(REAL), nullable=False),
    _ts("created_at"),
    schema=PRIVATE,
)

s3_messages = Table(
    "s3_messages",
    metadata,
    Column("message_id", Uuid, primary_key=True),
    _text("user_id"),
    _text("attempt_id"),
    _text("role"),  # user | steward
    Column("content", JSONB, nullable=False),
    _ts("created_at"),
    schema=PRIVATE,
)

response_cache = Table(
    "response_cache",
    metadata,
    Column("cache_key", Text, primary_key=True),
    _text("user_id"),
    _text("attempt_id"),
    Column("response", JSONB, nullable=False),
    _ts("created_at"),
    schema=PRIVATE,
)
