"""
The Steward (steward/, package prayog_steward) wired into the Prayog backend.

This module is the only place that knows both sides. It implements the Steward's nine
ports (steward/docs/INTEGRATION.md) against the platform's tables, registers the
run-seal hook on lifecycle.release(), and mounts the Steward API and UI on the app.

Mapping decisions (see steward/docs/INTEGRATION.md, "Prayog backend"):
- Run       = one allocation that has been released (a job can run more than once when
              it is requeued). run_id is the allocation id.
- Project   = (owner, project_ref). There is no projects table yet, so a project is
              scoped to its owner: two users who type the same project_ref never see
              each other's runs. project_id = "u<user id>:<project_ref or ->".
- Principal = the JWT's user. principal_id is str(users.id); ADMIN maps to the
              Platform Admin role; the session is (sub, exp) of the token.
- Job spec  = what a platform job carries: GPUs, walltime, snapshot. Proposals may
              change only gpu_count and walltime_minutes (settings.proposal_fields).
- Audit     = the platform's audit_events table (FR-INT-06).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import jwt
from fastapi import FastAPI
from prayog_steward.clock import Clock, SystemClock
from prayog_steward.config import StewardSettings
from prayog_steward.container import Ports, Steward, build
from prayog_steward.db.engine import make_engine
from prayog_steward.hooks import on_run_sealed
from prayog_steward.ports import (
    PLATFORM_ADMIN,
    AllocationRecord,
    AttemptView,
    AuditEvent,
    JobSpec,
    LedgerEntry,
    Principal,
    PrincipalKind,
    RebuildState,
    RunEnd,
    RunView,
    SubmissionReceipt,
    SubmissionRequest,
    TelemetryView,
)
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from steward_contracts import ReasoningPort

import models as orm
from database import DATABASE_URL, SessionLocal
from logic.auth import ALGORITHM, SECRET_KEY
from logic.errors import AppError
from logic.scheduler.domain import JobState, ReleaseReason
from logic.scheduler.lifecycle import ReleaseResult, register_release_hook
from logic.scheduler.requests import SubmissionRejected, format_walltime, submit_job

log = logging.getLogger("prayog.steward")

LOG_REF_PREFIX = "run_results:"
PROPOSAL_FIELDS = ["gpu_count", "walltime_minutes"]   # what submit_job() can change
_UNIT = re.compile(r"^(?P<node>.+)/gpu(?P<index>\d+)$")
_PROJECT = re.compile(r"^u(?P<owner>\d+):(?P<ref>.*)$")

RUN_END = {
    ReleaseReason.COMPLETED: RunEnd.SUCCEEDED,
    ReleaseReason.FAILED: RunEnd.FAILED,
    ReleaseReason.CANCELLED: RunEnd.CANCELLED,
    ReleaseReason.WALLTIME_EXCEEDED: RunEnd.WALLTIME_EXCEEDED,
    ReleaseReason.NODE_FAILURE: RunEnd.INTERRUPTED,
    ReleaseReason.DISPATCH_TIMEOUT: RunEnd.INTERRUPTED,
}
ATTEMPT_STATE = {
    JobState.IN_PROCESS: RebuildState.INITIATED,
    JobState.QUEUED: RebuildState.INITIATED,
    JobState.SCHEDULED: RebuildState.INITIATED,
    JobState.RUNNING: RebuildState.INITIATED,
    JobState.COMPLETED: RebuildState.SUCCESS,
    JobState.FAILED: RebuildState.FAILED,
    JobState.CANCELLED: RebuildState.CANCELLED,
    JobState.WALLTIME_EXCEEDED: RebuildState.CANCELLED,   # a timeout, §8.4
}


# ── identifiers ─────────────────────────────────────────────────────────

def aware(value: datetime | None) -> datetime | None:
    """Platform timestamps are naive UTC; the Steward's are timezone-aware."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def project_id(user_id: int, project_ref: str | None) -> str:
    return f"u{user_id}:{project_ref or '-'}"


def parse_project(value: str) -> tuple[int, str | None] | None:
    m = _PROJECT.match(value)
    if not m:
        return None
    ref = m["ref"]
    return int(m["owner"]), (None if ref == "-" else ref)


def unit_name(node_name: str, unit_index: int) -> str:
    return f"{node_name}/gpu{unit_index}"


def _int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── runs ────────────────────────────────────────────────────────────────

def run_view(session: Session, allocation_id: int) -> RunView | None:
    """A released allocation as a Steward run. None if it does not exist or is live."""
    row = session.execute(
        select(orm.Allocation, orm.Job, orm.Node.name, orm.Lab.name)
        .join(orm.Job, orm.Job.id == orm.Allocation.job_id)
        .join(orm.Node, orm.Node.id == orm.Allocation.node_id)
        .join(orm.Lab, orm.Lab.id == orm.Job.lab_id)
        .where(orm.Allocation.id == allocation_id)
    ).first()
    if row is None:
        return None
    allocation, job, node_name, lab_name = row
    if allocation.released_at is None or allocation.release_reason is None:
        return None
    reason = ReleaseReason(allocation.release_reason)
    units = session.execute(
        select(orm.GpuUnit.unit_index)
        .join(orm.AllocationUnit, orm.AllocationUnit.unit_id == orm.GpuUnit.id)
        .where(orm.AllocationUnit.allocation_id == allocation.id)
        .order_by(orm.GpuUnit.unit_index)
    ).scalars().all()
    result = session.get(orm.RunResult, allocation.id)
    attempt_id = session.scalar(
        select(orm.RebuildAttempt.id).where(orm.RebuildAttempt.job_id == job.id)
    )
    started = allocation.acknowledged_at or allocation.claimed_at
    return RunView(
        run_id=str(allocation.id),
        job_id=str(job.id),
        project_id=project_id(job.user_id, job.project_ref),
        owner_id=str(job.user_id),
        owner_lab=lab_name,
        node_id=node_name,
        unit_ids=tuple(unit_name(node_name, i) for i in units),
        end=RUN_END[reason],
        started_at=aware(started),  # type: ignore[arg-type]
        ended_at=aware(allocation.released_at),  # type: ignore[arg-type]
        job_spec=JobSpec(
            command_args="",                      # platform jobs carry no command yet
            env_pins=(),
            gpu_count=job.gpus,
            walltime_minutes=job.walltime_minutes,
            image_digest=job.snapshot_hash or "",
        ),
        exit_code=result.exit_code if result else None,
        failure_category=(result.failure_category if result and result.failure_category
                          else ("CRASH" if reason is ReleaseReason.FAILED else None)),
        interrupt_reason=reason.value if RUN_END[reason] is RunEnd.INTERRUPTED else None,
        log_refs=(f"{LOG_REF_PREFIX}{allocation.id}",) if result and result.log_tail else (),
        attempt_id=str(attempt_id) if attempt_id is not None else None,
    )


class PlatformRunReader:
    def __init__(self, sessions: Any = SessionLocal) -> None:
        self.sessions = sessions

    def get_run(self, run_id: str) -> RunView | None:
        allocation_id = _int(run_id)
        if allocation_id is None:
            return None
        with self.sessions() as session:
            return run_view(session, allocation_id)

    def earlier_runs(self, project: str, before: datetime, limit: int) -> Sequence[RunView]:
        parsed = parse_project(project)
        if parsed is None:
            return []
        owner, ref = parsed
        with self.sessions() as session:
            query = (
                select(orm.Allocation.id)
                .join(orm.Job, orm.Job.id == orm.Allocation.job_id)
                .where(
                    orm.Job.user_id == owner,
                    orm.Job.project_ref.is_(None) if ref is None else orm.Job.project_ref == ref,
                    orm.Allocation.released_at.is_not(None),
                    orm.Allocation.released_at < naive(before),
                )
                .order_by(orm.Allocation.released_at.desc())
                .limit(limit)
            )
            ids = session.scalars(query).all()
            return [v for i in ids if (v := run_view(session, i)) is not None]

    def telemetry(self, run_id: str) -> TelemetryView | None:
        return None                               # no metrics stream from agents yet


class PlatformLogStore:
    """Raw log tails in run_results. Only the Steward reads them, through its sanitiser."""

    def __init__(self, sessions: Any = SessionLocal) -> None:
        self.sessions = sessions

    def read_log(self, ref: str) -> str | None:
        if not ref.startswith(LOG_REF_PREFIX):
            return None
        allocation_id = _int(ref[len(LOG_REF_PREFIX):])
        if allocation_id is None:
            return None
        with self.sessions() as session:
            result = session.get(orm.RunResult, allocation_id)
            return result.log_tail if result else None


# ── identity and authorization ──────────────────────────────────────────

class JwtPrincipalResolver:
    """The platform's bearer token (logic.auth). Expired or invalid: no principal."""

    def __init__(self, sessions: Any = SessionLocal) -> None:
        self.sessions = sessions

    def resolve(self, credential: str) -> Principal | None:
        try:
            payload = jwt.decode(credential, SECRET_KEY, algorithms=[ALGORITHM])
            user_pk = int(payload["sub"])
            exp = int(payload["exp"])
        except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
            return None
        with self.sessions() as session:
            user = session.get(orm.User, user_pk)
            if user is None:
                return None
            lab = session.scalar(
                select(orm.Lab.name)
                .join(orm.SchedulingContext, orm.SchedulingContext.lab_id == orm.Lab.id)
                .where(orm.SchedulingContext.user_id == user.id)
            )
            roles = {user.role.lower()}
            if user.role == "ADMIN":
                roles.add(PLATFORM_ADMIN)
            return Principal(
                principal_id=str(user.id),
                kind=PrincipalKind.USER,
                lab=lab,
                roles=frozenset(roles),
                session_id=f"jwt:{user.id}:{exp}",
            )


class PlatformAuthorizer:
    def __init__(self, sessions: Any = SessionLocal, clock: Clock | None = None) -> None:
        self.sessions = sessions
        self.clock = clock or SystemClock()

    def authorization_active(self, user_id: str) -> bool:
        """Approved access that has not ended: a Researcher row, a Student row before
        its end date, or a professor/admin account (ID-03)."""
        uid = _int(user_id)
        if uid is None:
            return False
        now = naive(self.clock.now())
        with self.sessions() as session:
            user = session.get(orm.User, uid)
            if user is None:
                return False
            if user.role in ("PROFESSOR", "ADMIN"):
                return True
            if session.scalar(select(orm.Researcher.id).where(orm.Researcher.user_id == uid)):
                return True
            end = session.scalar(select(orm.Student.end_date).where(orm.Student.user_id == uid))
            return end is not None and end > now

    def may_submit(self, user_id: str, project: str) -> bool:
        """DEP-03 is open; the baseline is the project's owner."""
        parsed = parse_project(project)
        return parsed is not None and str(parsed[0]) == user_id

    def may_read_project(self, user_id: str, project: str) -> bool:
        """The owner, or the professor the owner's approved access names (FR-AUTH-63)."""
        parsed = parse_project(project)
        uid = _int(user_id)
        if parsed is None or uid is None:
            return False
        owner = parsed[0]
        if owner == uid:
            return True
        with self.sessions() as session:
            supervisor = session.scalar(
                select(orm.Professor.user_id).where(
                    orm.Professor.id.in_(
                        select(orm.Student.professor_id).where(orm.Student.user_id == owner)
                        .union(select(orm.Researcher.professor_id)
                               .where(orm.Researcher.user_id == owner))
                    )
                )
            )
            return supervisor == uid

    def session_active(self, session_id: str) -> bool:
        """The CLI's token is the session (no server-side sessions yet): active until it
        expires. A link dies with the token that requested it."""
        parts = session_id.split(":")
        if len(parts) != 3 or parts[0] != "jwt":
            return False
        exp = _int(parts[2])
        return exp is not None and exp > int(self.clock.now().timestamp())


# ── rebuilds, ledger, allocations ───────────────────────────────────────

class PlatformRebuildAttempts:
    def __init__(self, sessions: Any = SessionLocal) -> None:
        self.sessions = sessions

    def get(self, attempt_id: str) -> AttemptView | None:
        aid = _int(attempt_id)
        if aid is None:
            return None
        with self.sessions() as session:
            row = session.execute(
                select(orm.RebuildAttempt, orm.Job.state)
                .join(orm.Job, orm.Job.id == orm.RebuildAttempt.job_id)
                .where(orm.RebuildAttempt.id == aid)
            ).first()
            if row is None:
                return None
            attempt, job_state = row
            last_run = session.scalar(
                select(orm.Allocation.id)
                .where(orm.Allocation.job_id == attempt.job_id,
                       orm.Allocation.released_at.is_not(None))
                .order_by(orm.Allocation.released_at.desc())
            )
            return AttemptView(
                attempt_id=str(attempt.id),
                snapshot_id=attempt.snapshot_hash,
                project_id=project_id(attempt.user_id, attempt.project_ref),
                owner_id=str(attempt.user_id),
                state=ATTEMPT_STATE[JobState(job_state)],
                run_id=str(last_run) if last_run is not None else None,
            )


class PlatformLedgerHistory:
    """Until the ledger exists: the owner's finished jobs as run summaries."""

    def __init__(self, sessions: Any = SessionLocal) -> None:
        self.sessions = sessions

    def for_user(self, owner_id: str, project: str | None, limit: int) -> Sequence[LedgerEntry]:
        uid = _int(owner_id)
        if uid is None:
            return []
        ref_filter = None
        if project is not None:
            parsed = parse_project(project)
            if parsed is None or parsed[0] != uid:
                return []                         # never another owner's history
            ref_filter = parsed[1]
        terminal = (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED,
                    JobState.WALLTIME_EXCEEDED)
        with self.sessions() as session:
            query = select(orm.Job).where(orm.Job.user_id == uid, orm.Job.state.in_(terminal))
            if project is not None:
                query = query.where(orm.Job.project_ref.is_(None) if ref_filter is None
                                    else orm.Job.project_ref == ref_filter)
            jobs = session.scalars(query.order_by(orm.Job.id.desc()).limit(limit)).all()
            entries = []
            for job in jobs:
                gpu_seconds = session.scalar(
                    select(func.coalesce(func.sum(orm.UsageEntry.gpu_seconds), 0.0))
                    .where(orm.UsageEntry.job_id == job.id)
                )
                ended = session.scalar(
                    select(func.max(orm.Allocation.released_at)).where(orm.Allocation.job_id == job.id)
                ) or job.submitted_at
                entries.append(LedgerEntry(
                    entry_id=f"job-{job.id}",
                    owner_id=str(job.user_id),
                    project_id=project_id(job.user_id, job.project_ref),
                    kind="run_summary",
                    body=json.dumps({
                        "job_id": job.id,
                        "state": job.state,
                        "gpus": job.gpus,
                        "walltime_minutes": job.walltime_minutes,
                        "snapshot": job.snapshot_hash,
                        "gpu_hours_used": round(float(gpu_seconds) / 3600, 3),
                    }),
                    at=aware(ended),  # type: ignore[arg-type]
                ))
            return entries


class PlatformAllocationHistory:
    def __init__(self, sessions: Any = SessionLocal) -> None:
        self.sessions = sessions

    def for_units(self, unit_ids: Sequence[str], since: datetime) -> Sequence[AllocationRecord]:
        records: list[AllocationRecord] = []
        with self.sessions() as session:
            for name in unit_ids:
                m = _UNIT.match(name)
                if not m:
                    continue
                rows = session.execute(
                    select(orm.Allocation.job_id, orm.Lab.name, orm.Allocation.claimed_at,
                           orm.Allocation.released_at)
                    .join(orm.AllocationUnit, orm.AllocationUnit.allocation_id == orm.Allocation.id)
                    .join(orm.GpuUnit, orm.GpuUnit.id == orm.AllocationUnit.unit_id)
                    .join(orm.Node, orm.Node.id == orm.GpuUnit.node_id)
                    .join(orm.Job, orm.Job.id == orm.Allocation.job_id)
                    .join(orm.Lab, orm.Lab.id == orm.Job.lab_id)
                    .where(orm.Node.name == m["node"], orm.GpuUnit.unit_index == int(m["index"]))
                    .where((orm.Allocation.released_at.is_(None))
                           | (orm.Allocation.released_at >= naive(since)))
                    .order_by(orm.Allocation.claimed_at)
                ).all()
                records += [
                    AllocationRecord(name, str(job_id), lab, aware(start), aware(end))  # type: ignore[arg-type]
                    for job_id, lab, start, end in rows
                ]
        return records


# ── submission and audit ────────────────────────────────────────────────

class PlatformSubmissions:
    """An approved proposal is an ordinary submission (FR-GPU-150): same rules, a new
    submit time, no priority advantage. The proposal ID is the client_request_id, so a
    retried approval returns the same job (FR-GPU-107)."""

    def __init__(self, sessions: Any = SessionLocal, trigger_pass: bool = True) -> None:
        self.sessions = sessions
        self.trigger_pass = trigger_pass

    def submit(self, request: SubmissionRequest) -> SubmissionReceipt:
        user = _int(request.approving_user)
        parsed = parse_project(request.project_id)
        if user is None or parsed is None:
            return SubmissionReceipt(accepted=False, reason="unknown user or project")
        spec = request.job_spec
        with self.sessions() as session:
            try:
                result = submit_job(
                    session,
                    user,
                    gpus=spec.gpu_count,
                    walltime=format_walltime(spec.walltime_minutes),
                    client_request_id=f"steward:{request.idempotency_key}"[:64],
                    project_ref=parsed[1],
                    snapshot_hash=spec.image_digest or None,
                )
            except (SubmissionRejected, AppError) as exc:
                session.rollback()
                return SubmissionReceipt(accepted=False, reason=str(exc))
            job_id = result.job.id
        if self.trigger_pass:
            _trigger_pass("submit")
        return SubmissionReceipt(accepted=True, job_id=str(job_id))


class PlatformAuditSink:
    """The platform's single append-only audit log (FR-INT-06)."""

    def __init__(self, sessions: Any = SessionLocal) -> None:
        self.sessions = sessions

    def append(self, event: AuditEvent) -> None:
        with self.sessions() as session:
            session.add(orm.AuditEvent(
                at=naive(event.at),
                actor=event.actor[:100],
                actor_kind=event.actor_kind.value,
                operation=event.operation[:100],
                resource_ids=[r[:200] for r in event.resource_ids],
                outcome=event.outcome[:30],
                reason=event.reason[:200] if event.reason else None,
            ))
            session.commit()


def _trigger_pass(trigger: str) -> None:
    """Run a scheduling pass off the request thread (FR-GPU-121)."""
    def run() -> None:
        try:
            from logic.scheduler.passes import run_pass
            from routers.scheduler.jobs import ACTIVE_CONFIG

            run_pass(SessionLocal, trigger, cfg=ACTIVE_CONFIG)
        except Exception:
            log.exception("scheduling pass after a Steward submission failed")

    threading.Thread(target=run, name="steward-pass", daemon=True).start()


# ── composition ─────────────────────────────────────────────────────────

def steward_settings() -> StewardSettings:
    """STEWARD_* environment, completed from the platform's own configuration:
    the database host comes from POSTGRES_*, the Steward connects as its own
    non-superuser role (STEWARD_DB_USER / STEWARD_DB_PASSWORD) so row-level security
    applies, and maintenance work (retention purge, health counts) uses the owner."""
    overrides: dict[str, Any] = {
        "platform_login_url": os.getenv("STEWARD_PLATFORM_LOGIN_URL", "/auth/login"),
        "proposal_fields": PROPOSAL_FIELDS,
    }
    if not os.getenv("STEWARD_DATABASE_URL"):
        password = os.getenv("STEWARD_DB_PASSWORD")
        if not password:
            raise RuntimeError("STEWARD_DB_PASSWORD is not set (see backend/scripts/setup_steward.py)")
        overrides["database_url"] = URL.create(
            "postgresql+psycopg",
            username=os.getenv("STEWARD_DB_USER", "steward_app"),
            password=password,
            host=DATABASE_URL.host,
            port=DATABASE_URL.port,
            database=DATABASE_URL.database,
        ).render_as_string(hide_password=False)
    if not os.getenv("STEWARD_MAINTENANCE_DATABASE_URL"):
        overrides["maintenance_database_url"] = DATABASE_URL.render_as_string(hide_password=False)
    return StewardSettings(**overrides)


def steward_ports() -> Ports:
    """Also usable as an entry-point factory (--ports integration.steward_bridge:steward_ports)."""
    return Ports(
        runs=PlatformRunReader(),
        logs=PlatformLogStore(),
        principals=JwtPrincipalResolver(),
        authorizer=PlatformAuthorizer(),
        attempts=PlatformRebuildAttempts(),
        submissions=PlatformSubmissions(),
        audit=PlatformAuditSink(),
        allocations=PlatformAllocationHistory(),
        ledger=PlatformLedgerHistory(),
    )


def reasoning_from_env(settings: StewardSettings) -> ReasoningPort | None:
    """STEWARD_INPROCESS_REASONING=heuristic|stub|claude runs the model adapter inside
    this process (development, tests, single-machine setups). Unset: the separately
    deployed Runtime at STEWARD_RUNTIME_URL (SADD ADR-02)."""
    mode = os.getenv("STEWARD_INPROCESS_REASONING", "").strip().lower()
    if not mode:
        return None
    if mode == "heuristic":
        from steward_runtime.heuristic import HeuristicReasoning

        return HeuristicReasoning()
    if mode == "stub":
        from steward_runtime.stub import StubReasoning

        return StubReasoning()
    if mode == "claude":
        from steward_runtime.claude import ClaudeReasoning

        return ClaudeReasoning(model=os.getenv("STEWARD_MODEL", "claude-opus-5-5"))
    raise RuntimeError(f"STEWARD_INPROCESS_REASONING must be heuristic, stub or claude, not {mode!r}")


@lru_cache(maxsize=1)
def get_steward() -> Steward:
    settings = steward_settings()
    return build(
        settings,
        steward_ports(),
        engine=make_engine(settings.database_url),
        reasoning=reasoning_from_env(settings),
    )


def seal_hook(session: Session, result: ReleaseResult, reason: ReleaseReason, now: datetime) -> None:
    """Record the end of a run for the Steward, inside the release transaction
    (SADD §8.3): failure event, fault evidence and the S1 task commit with the release.
    Runs in a savepoint (lifecycle._run_release_hooks): a failure here never blocks
    the release."""
    steward = get_steward()
    session.flush()
    run = run_view(session, result.allocation_id)
    if run is None:
        return
    log_tail = None
    if run.log_refs:
        stored = session.get(orm.RunResult, result.allocation_id)
        log_tail = stored.log_tail if stored else None
    on_run_sealed(session.connection(), run, settings=steward.settings, clock=steward.clock,
                  log_tail=log_tail)


def register_steward_hooks() -> bool:
    """Only the run-seal hook, for processes that release runs but serve no HTTP (the
    scheduler tick, a future agent gateway). Every process that calls
    lifecycle.release() must call this or install_steward(), or the Steward misses
    those run ends."""
    if os.getenv("STEWARD_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        return False
    try:
        get_steward()
    except Exception as exc:  # noqa: BLE001 - a misconfigured Steward must not stop scheduling
        log.warning("Steward hooks not registered: %s", exc)
        return False
    register_release_hook(seal_hook)
    return True


def install_steward(app: FastAPI) -> bool:
    """Mount the Steward on the app and hook it into run releases. If it is not
    configured (STEWARD_ENABLED=false, or no database role), the platform runs without
    it: nothing in scheduling depends on the Steward (NFR-GPU-055)."""
    if os.getenv("STEWARD_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        log.info("Steward disabled by STEWARD_ENABLED")
        return False
    try:
        steward = get_steward()
    except Exception as exc:  # noqa: BLE001 - a misconfigured Steward must not stop the platform
        log.warning("Steward not started: %s", exc)
        return False
    from prayog_steward.api.app import mount

    mount(app, steward)
    register_release_hook(seal_hook)
    _warm_knowledge(steward)
    return True


def _warm_knowledge(steward: Steward) -> None:
    """Load the knowledge index and embedding model off the request path. A semantic
    embedder takes seconds to load; without this the first S3/S4 request pays for it."""
    if steward.knowledge is None:
        return
    knowledge = steward.knowledge

    def warm() -> None:
        try:
            if knowledge.size:
                knowledge.embedder.embed_query(["warm up"])
        except Exception:
            log.warning("knowledge warm-up failed", exc_info=True)

    threading.Thread(target=warm, name="steward-knowledge-warmup", daemon=True).start()


__all__ = [
    "get_steward",
    "install_steward",
    "register_steward_hooks",
    "seal_hook",
    "steward_ports",
    "steward_settings",
]
