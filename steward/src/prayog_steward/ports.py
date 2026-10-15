"""Contracts with modules owned by other sub-teams (ADR-S07).

Every dependency on run sealing, identity, the ledger, rebuild, the Request Service,
the audit log and allocation history goes through one of these protocols. The real
implementations come from the owning teams; ``prayog_steward.fakes`` stands in until then.
Identifiers from other subsystems are opaque strings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

# ---------------------------------------------------------------- shared views


class RunEnd(StrEnum):
    """Terminal run outcomes (Integrated Spec §8.3)."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WALLTIME_EXCEEDED = "WALLTIME_EXCEEDED"
    INTERRUPTED = "INTERRUPTED"


class RebuildState(StrEnum):
    """Rebuild attempt states (§8.4). REBUILD_REJECTED never gets an attempt ID."""

    INITIATED = "REBUILD_INITIATED"
    SUCCESS = "REBUILD_SUCCESS"
    FAILED = "REBUILD_FAILED"
    CANCELLED = "REBUILD_CANCELLED"


class PrincipalKind(StrEnum):
    """FR-AUTH-74: users, node agents and service identities share one enforcement path."""

    USER = "user"
    NODE = "node"
    SERVICE = "service"


@dataclass(frozen=True)
class JobSpec:
    command_args: str
    env_pins: tuple[str, ...]
    gpu_count: int
    walltime_minutes: int
    image_digest: str
    batch_size: int | None = None
    precision: str | None = None

    @property
    def requested_gpu_hours(self) -> float:
        """What the owner is shown before approving a proposal (FR-GPU-187)."""
        return self.gpu_count * self.walltime_minutes / 60


@dataclass(frozen=True)
class RunView:
    run_id: str
    job_id: str
    project_id: str
    owner_id: str
    owner_lab: str
    node_id: str
    unit_ids: tuple[str, ...]
    end: RunEnd
    started_at: datetime
    ended_at: datetime
    job_spec: JobSpec
    exit_code: int | None = None
    failure_category: str | None = None
    interrupt_reason: str | None = None  # "NODE_FAILURE" makes it fault evidence (FR-GPU-147)
    log_refs: tuple[str, ...] = ()
    attempt_id: str | None = None  # set when the job is a rebuild attempt (FR-INT-01)


@dataclass(frozen=True)
class TelemetryView:
    run_id: str
    summary: dict[str, float]  # e.g. peak_memory_gb, mean_util_pct; numbers only


PLATFORM_ADMIN = "platform_admin"  # role name; the access sub-team owns the vocabulary (ID-01)


@dataclass(frozen=True)
class Principal:
    principal_id: str
    kind: PrincipalKind
    lab: str | None = None
    roles: frozenset[str] = frozenset()
    session_id: str | None = None  # the authenticated session this request belongs to

    @property
    def is_platform_admin(self) -> bool:
        return self.kind is PrincipalKind.USER and PLATFORM_ADMIN in self.roles


@dataclass(frozen=True)
class AttemptView:
    attempt_id: str
    snapshot_id: str
    project_id: str
    owner_id: str
    state: RebuildState
    run_id: str | None = None


@dataclass(frozen=True)
class SubmissionRequest:
    """An approved proposal, submitted through the ordinary path (FR-GPU-150)."""

    project_id: str
    job_spec: JobSpec
    parent_run_id: str
    approving_user: str
    idempotency_key: str  # the proposal ID: a retried approval never queues twice


@dataclass(frozen=True)
class SubmissionReceipt:
    accepted: bool
    job_id: str | None = None
    reason: str | None = None  # e.g. quota exceeded; shown like any rejected submission


@dataclass(frozen=True)
class AuditEvent:
    """One record of the single append-only audit log (FR-INT-06). Identifiers only,
    never content (FR-INT-07)."""

    actor: str
    actor_kind: PrincipalKind
    operation: str
    resource_ids: tuple[str, ...]
    outcome: str  # "ok", "refused", "denied", ...
    at: datetime
    reason: str | None = None


@dataclass(frozen=True)
class AllocationRecord:
    unit_id: str
    job_id: str
    owner_lab: str
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    owner_id: str
    project_id: str
    kind: str  # "metrics", "config", "run_summary", ...
    body: str
    at: datetime
    attempt_id: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------- ports


class RunReader(Protocol):
    """Results Service / scheduler (GPU sub-team)."""

    def get_run(self, run_id: str) -> RunView | None: ...

    def earlier_runs(self, project_id: str, before: datetime, limit: int) -> Sequence[RunView]:
        """The same project's runs that ended before ``before``, newest first (FR-GPU-148)."""
        ...

    def telemetry(self, run_id: str) -> TelemetryView | None: ...


class LogStore(Protocol):
    """Content store. Raw logs never leave the server unsanitised (FR-STW-011, ADR-S03)."""

    def read_log(self, ref: str) -> str | None: ...


class PrincipalResolver(Protocol):
    """Access sub-team. Turns a presented credential into a principal, or None."""

    def resolve(self, credential: str) -> Principal | None: ...


class Authorizer(Protocol):
    """Access sub-team. Server-side decisions only (ID-03, ADR-A7)."""

    def authorization_active(self, user_id: str) -> bool:
        """False after expiry or revocation: commitment operations such as approving a
        proposal are refused, while retrieval still works (ID-03)."""
        ...

    def may_submit(self, user_id: str, project_id: str) -> bool:
        """DEP-03 submit predicate (open; owned by the ledger sub-team)."""
        ...

    def may_read_project(self, user_id: str, project_id: str) -> bool:
        """Owner, or the Professor supervising the owner (FR-AUTH-63, ID-07). Never a
        Platform Admin by virtue of the role (NFR-AUTH-18)."""
        ...

    def session_active(self, session_id: str) -> bool:
        """False after logout or revocation (ID-12). Binds Steward links to a live session."""
        ...


class RebuildAttempts(Protocol):
    """Ledger sub-team (rebuild state machine, §8.4)."""

    def get(self, attempt_id: str) -> AttemptView | None: ...


class SubmissionPort(Protocol):
    """Request Service: validation, quota, eligibility, class and ranking (FR-GPU-150)."""

    def submit(self, request: SubmissionRequest) -> SubmissionReceipt: ...


class AuditSink(Protocol):
    """The single append-only audit log (FR-INT-06)."""

    def append(self, event: AuditEvent) -> None: ...


class AllocationHistory(Protocol):
    """Allocation history of units, for the admin's warning view (FR-GPU-158)."""

    def for_units(self, unit_ids: Sequence[str], since: datetime) -> Sequence[AllocationRecord]: ...


class LedgerHistory(Protocol):
    """The requesting user's own ledger evidence (S3, S4). Implementations must filter by
    owner; the Steward re-checks ownership as well (defence in depth, NFR-SEC-002)."""

    def for_user(
        self, owner_id: str, project_id: str | None, limit: int
    ) -> Sequence[LedgerEntry]: ...
