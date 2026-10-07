"""Table definitions used by queries. The DDL of record is the Alembic migrations, which
also install row-level security and append-only triggers that SQLAlchemy cannot express.
``tests/integration/test_schema.py`` checks the two stay in step.

Identifiers owned by other subsystems (runs, jobs, users, nodes, units, attempts) are
opaque ``text``. Our own identifiers are UUIDs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
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
from sqlalchemy.types import UserDefinedType

STEWARD = "steward"
PRIVATE = "steward_private"
EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5 (ADR-S06)

metadata = MetaData()


class Vector(UserDefinedType[list[float]]):
    """Minimal pgvector column type, so table definitions do not need the pgvector package."""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **_: object) -> str:
        return f"vector({self.dim})"


def _ts(name: str, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


# ------------------------------------------------------------------ steward (S1, S2)

steward_tasks = Table(
    "steward_tasks",
    metadata,
    Column("task_id", Uuid, primary_key=True),
    Column("context", Text, nullable=False),  # S1 | S2
    Column("subject_id", Text, nullable=False),  # run_id for S1, warning_id for S2
    Column("state", Text, nullable=False),  # PENDING | RUNNING | DONE | GAVE_UP
    Column("attempts", Integer, nullable=False),
    _ts("next_attempt_at"),
    Column("last_error", Text),
    _ts("created_at"),
    _ts("updated_at"),
    schema=STEWARD,
)

failure_events = Table(
    "failure_events",
    metadata,
    Column("event_id", Uuid, primary_key=True),
    Column("run_id", Text, nullable=False, unique=True),
    Column("job_id", Text, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("unit_ids", ARRAY(Text), nullable=False),
    Column("exit_code", Integer),
    Column("failure_category", Text, nullable=False),
    Column("log_refs", ARRAY(Text), nullable=False),
    _ts("at"),
    schema=STEWARD,
)

fault_evidence = Table(
    "fault_evidence",
    metadata,
    Column("evidence_id", Uuid, primary_key=True),
    Column("run_id", Text, nullable=False, unique=True),
    Column("job_id", Text, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("unit_id", Text, nullable=False),
    Column("owner_lab", Text, nullable=False),
    # Needed to count distinct users (FR-GPU-162). An identifier, never shown to S2.
    Column("owner_id", Text, nullable=False),
    Column("exit_code", Integer),
    Column("failure_category", Text, nullable=False),
    Column("failure_signature", Text),  # ADR-S05, pending CR-S1
    _ts("at"),
    _ts("consumed_at", nullable=True),
    schema=STEWARD,
)

diagnoses = Table(
    "diagnoses",
    metadata,
    Column("diagnosis_id", Uuid, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("outcome", Text, nullable=False),  # ok | refused | malformed | unavailable
    Column("classification", Text),
    Column("claims", JSONB, nullable=False),
    Column("matching_earlier_failures", ARRAY(Text), nullable=False),
    Column("stripped_count", Integer, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("agent_version", Text, nullable=False),
    Column("model", Text),
    _ts("created_at"),
    schema=STEWARD,
)

hardware_findings = Table(
    "hardware_findings",
    metadata,
    Column("finding_id", Uuid, primary_key=True),
    Column("diagnosis_id", Uuid, ForeignKey(f"{STEWARD}.diagnoses.diagnosis_id"), nullable=False),
    Column("run_id", Text, nullable=False),
    Column("verdict", Text, nullable=False),
    Column("indicators", ARRAY(Text), nullable=False),
    _ts("created_at"),
    schema=STEWARD,
)

proposals = Table(
    "proposals",
    metadata,
    Column("proposal_id", Uuid, primary_key=True),
    Column("diagnosis_id", Uuid, ForeignKey(f"{STEWARD}.diagnoses.diagnosis_id"), nullable=False),
    Column("run_id", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("changes", JSONB, nullable=False),  # the model's JobChange list, citations kept
    Column("job_spec", JSONB, nullable=False),  # the exact resulting spec (FR-GPU-187)
    Column("requested_gpu_hours", Numeric(10, 2), nullable=False),
    Column("state", Text, nullable=False),  # PROPOSED | APPROVED | REJECTED
    Column("decided_by", Text),
    _ts("decided_at", nullable=True),
    Column("submitted_job_id", Text),
    Column("submission_refusal", Text),
    _ts("created_at"),
    schema=STEWARD,
)

hardware_warnings = Table(
    "hardware_warnings",
    metadata,
    Column("warning_id", Uuid, primary_key=True),
    Column("unit_signature", Text, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("state", Text, nullable=False),  # OPEN | RESOLVED | DISMISSED
    _ts("opened_at"),
    _ts("closed_at", nullable=True),
    Column("closed_by", Text),
    Column("close_reason", Text),
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
# Row-level security on every table: user_id = current_setting('app.user_id') (ADR-19).

steward_links = Table(
    "steward_links",
    metadata,
    Column("token_hash", LargeBinary, primary_key=True),
    Column("attempt_id", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    _ts("created_at"),
    _ts("expires_at"),
    _ts("consumed_at", nullable=True),
    schema=PRIVATE,
)

rag_chunks = Table(
    "rag_chunks",
    metadata,
    Column("chunk_id", Uuid, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("attempt_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("body", Text, nullable=False),  # sanitised (ADR-S03)
    Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    _ts("created_at"),
    schema=PRIVATE,
)

s3_messages = Table(
    "s3_messages",
    metadata,
    Column("message_id", Uuid, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("attempt_id", Text, nullable=False),
    Column("role", Text, nullable=False),  # user | steward
    Column("content", JSONB, nullable=False),
    _ts("created_at"),
    schema=PRIVATE,
)

response_cache = Table(
    "response_cache",
    metadata,
    Column("cache_key", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("attempt_id", Text, nullable=False),
    Column("response", JSONB, nullable=False),
    _ts("created_at"),
    schema=PRIVATE,
)
