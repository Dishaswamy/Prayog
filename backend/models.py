from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)

# Current time in UTC (Postgres now() otherwise follows the server's timezone)
UTC_NOW = text("(now() AT TIME ZONE 'utc')")
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from logic.scheduler.domain import AccountType, DonationMode, JobState, NodeState, ReleaseReason


class Base(DeclarativeBase):
    pass


# Allowed values (kept in one place so the DB and the code agree)
ROLES = "('STUDENT', 'RESEARCHER', 'PROFESSOR', 'ADMIN')"
DECISIONS = "('PENDING', 'APPROVED', 'REJECTED')"
FINAL_STATUSES = "('PENDING', 'APPROVED', 'REJECTED', 'REVOKED')"


# ─────────────────────────────────────────────
# Registered app users (every login is a row here)
# ─────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"role IN {ROLES}", name="ck_users_role"),
    )

    # Internal integer key: used in foreign keys and in the JWT "sub"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Public ID = part of the IITK email before "@"
    user_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )


# ─────────────────────────────────────────────
# Professor profile (one per PROFESSOR user, created by an admin)
# ─────────────────────────────────────────────
class Professor(Base):
    __tablename__ = "professors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), unique=True, nullable=False
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )


# ─────────────────────────────────────────────
# Access request + both approvals
# ─────────────────────────────────────────────
class AccessRequest(Base):
    __tablename__ = "access_requests"
    __table_args__ = (
        CheckConstraint("role IN ('STUDENT', 'RESEARCHER')", name="ck_ar_role"),
        CheckConstraint(f"status_professor IN {DECISIONS}", name="ck_ar_status_professor"),
        CheckConstraint(f"status_admin IN {DECISIONS}", name="ck_ar_status_admin"),
        CheckConstraint(f"final_status IN {FINAL_STATUSES}", name="ck_ar_final_status"),
        # Students: end date required and after start. Researchers: no end date.
        CheckConstraint(
            "(role = 'RESEARCHER' AND end_date IS NULL) OR "
            "(role = 'STUDENT' AND end_date IS NOT NULL AND end_date > start_date)",
            name="ck_ar_dates",
        ),
        Index("ix_ar_user_status", "user_id", "final_status"),
        Index("ix_ar_professor_status", "professor_id", "final_status"),
        Index("ix_ar_roll_number", "roll_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Applicant's registered account
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    course: Mapped[str] = mapped_column(String(100), nullable=False)
    branch: Mapped[str] = mapped_column(String(100), nullable=False)

    # Resolved from the professor email submitted in the request
    professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), nullable=False)

    # Required for both students and researchers
    roll_number: Mapped[str] = mapped_column(String(50), nullable=False)

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # NULL for researchers

    status_professor: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    status_admin: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    final_status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, onupdate=func.timezone("utc", func.now()), nullable=False
    )


# ─────────────────────────────────────────────
# Approved students (row exists only while access is APPROVED)
# ─────────────────────────────────────────────
class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    roll_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    course: Mapped[str] = mapped_column(String(100), nullable=False)
    branch: Mapped[str] = mapped_column(String(100), nullable=False)

    professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), nullable=False)

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )


# ─────────────────────────────────────────────
# Approved researchers (row exists only while access is APPROVED)
# ─────────────────────────────────────────────
class Researcher(Base):
    __tablename__ = "researchers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    roll_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    course: Mapped[str] = mapped_column(String(100), nullable=False)
    branch: Mapped[str] = mapped_column(String(100), nullable=False)

    professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), nullable=False)

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )


# ─────────────────────────────────────────────
# GPU scheduling (GPU requirements sections 6 and 7.4)
#
# Enum values are stored as strings with a CHECK constraint, like the tables
# above. Two partial unique indexes are the database's own guarantee against
# double allocation, whatever the code does:
#   I1  at most one live allocation per unit   ux_alloc_units_live  (FR-GPU-055)
#   I2  at most one live allocation per job    ux_allocations_live
# A unit is free when its current_allocation_id is NULL. The claim locks the
# unit row and decides on that column, so whoever releases an allocation must
# clear it in the same transaction as setting released_at.
# ─────────────────────────────────────────────
LIVE = text("released_at IS NULL")


def _one_of(enum: type[StrEnum]) -> str:
    return "(" + ", ".join(f"'{member.value}'" for member in enum) + ")"


class Lab(Base):
    __tablename__ = "labs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # The institute itself, owner of the teaching pool
    is_central_owner: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)


class SchedulingContext(Base):
    """
    The lab and account type a user acts under. Stopgap until the authorization
    record carries them (PRAYOG-27); the scheduler reads only this table, so the
    switch later is one query.
    """
    __tablename__ = "scheduling_contexts"
    __table_args__ = (
        CheckConstraint(f"account_type IN {_one_of(AccountType)}", name="ck_sc_account_type"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    lab_id: Mapped[int] = mapped_column(ForeignKey("labs.id"), nullable=False)
    account_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Sponsorship group cap for course accounts (Sprint 2); NULL = no cap
    course_cap_gpu_hours: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (
        CheckConstraint(f"donation_mode IN {_one_of(DonationMode)}", name="ck_nodes_donation_mode"),
        CheckConstraint(f"state IN {_one_of(NodeState)}", name="ck_nodes_state"),
        # Only shared nodes may be in the teaching pool (FR-GPU-181)
        CheckConstraint("NOT teaching_pool OR donation_mode = 'shared'", name="ck_nodes_teaching_shared"),
        CheckConstraint("unit_count >= 1", name="ck_nodes_unit_count"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    owner_lab_id: Mapped[int] = mapped_column(ForeignKey("labs.id"), nullable=False)
    donation_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    teaching_pool: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    state: Mapped[str] = mapped_column(String(20), server_default=NodeState.PENDING_AGENT.value, nullable=False)
    # Declared at registration; the agent's reported count must match it (FR-GPU-110)
    unit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GpuUnit(Base):
    __tablename__ = "gpu_units"
    __table_args__ = (
        UniqueConstraint("node_id", "unit_index", name="uq_gpu_units_node_index"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    unit_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # The claim marker: NULL = free. Set and cleared together with the allocation.
    current_allocation_id: Mapped[int | None] = mapped_column(ForeignKey("allocations.id"), nullable=True)


class MaintenanceWindow(Base):
    """A window on one unit; a freeze is a window that starts immediately."""
    __tablename__ = "maintenance_windows"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_mw_order"),
        Index("ix_mw_unit_ends", "unit_id", "ends_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("gpu_units.id"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(f"state IN {_one_of(JobState)}", name="ck_jobs_state"),
        CheckConstraint(f"account_type IN {_one_of(AccountType)}", name="ck_jobs_account_type"),
        CheckConstraint("gpus >= 1", name="ck_jobs_gpus"),
        CheckConstraint("walltime_minutes >= 1", name="ck_jobs_walltime"),
        # A retried submission returns the original job (FR-GPU-107)
        UniqueConstraint("user_id", "client_request_id", name="uq_jobs_client_request"),
        Index("ix_jobs_state_lab", "state", "lab_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    project_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)   # projects table not built yet
    snapshot_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Acting context, stamped from scheduling_contexts at submission, never from the client
    lab_id: Mapped[int] = mapped_column(ForeignKey("labs.id"), nullable=False)
    account_type: Mapped[str] = mapped_column(String(20), nullable=False)
    gpus: Mapped[int] = mapped_column(Integer, nullable=False)
    walltime_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    state_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Audit time of the submission, never changed
    submitted_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)
    # The age clock: submit time as adjusted by the edit rules (FR-GPU-071/072)
    effective_submit_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Allocation(Base):
    """One claim: a job's units on one node (single-node only, FR-GPU-183)."""
    __tablename__ = "allocations"
    __table_args__ = (
        Index("ux_allocations_live", "job_id", unique=True, postgresql_where=LIVE),   # I2
        CheckConstraint(f"release_reason IN {_one_of(ReleaseReason)}", name="ck_alloc_release_reason"),
        CheckConstraint("(released_at IS NULL) = (release_reason IS NULL)", name="ck_alloc_released_with_reason"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)          # FR-GPU-057
    # Set when the agent acknowledges the dispatch; still NULL after dispatch_ack_timeout
    # means the claim is released and the job requeued (FR-GPU-126)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # FR-GPU-057
    release_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)


class AllocationUnit(Base):
    __tablename__ = "allocation_units"
    __table_args__ = (
        Index("ux_alloc_units_live", "unit_id", unique=True, postgresql_where=LIVE),  # I1, FR-GPU-055
    )

    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("gpu_units.id"), primary_key=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SchedulingDecision(Base):
    """
    Every placement with all its inputs, so the score can be recomputed exactly
    (FR-GPU-098, NFR-GPU-008). Insert-only. A, F, C and S are stored as doubles,
    the same values the score was computed from.
    """
    __tablename__ = "scheduling_decisions"
    __table_args__ = (
        CheckConstraint("priority_class BETWEEN 0 AND 3", name="ck_sd_class"),
        Index("ix_sd_job", "job_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pass_id: Mapped[str] = mapped_column(String(36), nullable=False)
    trigger: Mapped[str] = mapped_column(String(30), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), nullable=False)  # gives the units
    priority_class: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    age: Mapped[float] = mapped_column(Double, nullable=False)
    fair_share: Mapped[float | None] = mapped_column(Double, nullable=True)      # NULL for course accounts
    contribution: Mapped[float | None] = mapped_column(Double, nullable=True)
    size: Mapped[float] = mapped_column(Double, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)


class SchedulingPass(Base):
    """One row per scheduling pass: what it saw and did (A-74, NFR-GPU-051). Insert-only."""
    __tablename__ = "scheduling_passes"
    __table_args__ = (
        Index("ix_sp_started_at", "started_at"),
    )

    pass_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Double, nullable=False)
    queued: Mapped[int] = mapped_column(Integer, nullable=False)
    placed: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_failures: Mapped[int] = mapped_column(Integer, nullable=False)


class UsageEntry(Base):
    """
    GPU time of one run, written when its allocation is released: units x
    (confirmed end - confirmed start), FR-GPU-100. Charged runs count towards the
    consumer lab's fair share and consumption; an interrupted run is not charged,
    but a guest run always counts as served for the host lab (FR-GPU-143).
    """
    __tablename__ = "usage_entries"
    __table_args__ = (
        CheckConstraint("gpu_seconds >= 0", name="ck_usage_gpu_seconds"),
        Index("ix_usage_ended_at", "ended_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    consumer_lab_id: Mapped[int] = mapped_column(ForeignKey("labs.id"), nullable=False)
    provider_lab_id: Mapped[int] = mapped_column(ForeignKey("labs.id"), nullable=False)
    gpu_seconds: Mapped[float] = mapped_column(Double, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    charged: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)


# ─────────────────────────────────────────────
# Steward integration (SADD §8; Integrated Spec §11.6, FR-INT-01, FR-INT-06)
# ─────────────────────────────────────────────
class RunResult(Base):
    """
    What the node agent reported when a run ended (FR-GPU-146): exit code, failure
    category and the tail of its log. Written by lifecycle.release() in the release
    transaction. Stand-in for the content store until it exists: no API serves this
    table; the Steward reads the log only through its sanitiser (ADR-S03).
    """
    __tablename__ = "run_results"

    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)


class RebuildAttempt(Base):
    """
    A rebuild of a recorded snapshot (FR-ML-003, §8.4), executed as an ordinary job
    (FR-INT-01). Interim implementation owned by the ledger sub-team: the attempt's
    state is derived from its job's state, so it can never disagree with the scheduler.
    REBUILD_REJECTED never creates a row (no Attempt ID).
    """
    __tablename__ = "rebuild_attempts"
    __table_args__ = (
        Index("ix_rebuild_attempts_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    project_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTC_NOW, nullable=False)


class AuditEvent(Base):
    """
    The single append-only audit log across subsystems (FR-INT-06). Identifiers,
    outcome and a short reason only, never content (FR-INT-07). A trigger refuses
    UPDATE and DELETE for every role.
    """
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("actor_kind IN ('user', 'node', 'service')", name="ck_audit_actor_kind"),
        Index("ix_audit_events_at", "at"),
        Index("ix_audit_events_actor", "actor"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_ids: Mapped[list[str]] = mapped_column(ARRAY(String(200)), nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
