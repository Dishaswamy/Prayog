"""Owner-facing S1 services: diagnoses and proposal approval (UC-GPU-20, ST-10).

Visibility (SADD §8.3 table):
- a diagnosis narrative is visible to the run owner, and read-only to a supervising
  Professor (``Authorizer.may_read_project``). Never to a Platform Admin as such;
- a proposal is visible to, and decided by, the owner only;
- anything not visible is reported as not found, the same as a non-existent ID
  (NFR-AUTH-17).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select, update

from prayog_steward.audit import Auditor
from prayog_steward.clock import Clock
from prayog_steward.db.models import diagnoses, hardware_findings, proposals
from prayog_steward.ports import (
    Authorizer,
    JobSpec,
    Principal,
    RunReader,
    SubmissionPort,
    SubmissionRequest,
)


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Forbidden(Exception):
    pass


@dataclass(frozen=True)
class DiagnosisView:
    diagnosis_id: str
    run_id: str
    outcome: str
    classification: str | None
    claims: list[dict[str, Any]]
    matching_earlier_failures: list[str]
    stripped_count: int
    hardware_verdict: str | None
    hardware_indicators: list[str]
    agent_version: str
    created_at: datetime


@dataclass(frozen=True)
class DiagnosisSummary:
    """One row of the dashboard list; the narrative is fetched per run."""
    diagnosis_id: str
    run_id: str
    project_id: str
    owner_id: str
    mine: bool
    outcome: str
    classification: str | None
    proposal_state: str | None  # the owner's only, like the hardware finding
    created_at: datetime


@dataclass(frozen=True)
class ProposalView:
    proposal_id: str
    run_id: str
    diagnosis_id: str
    state: str
    changes: list[dict[str, Any]]
    current_spec: dict[str, Any] | None
    proposed_spec: dict[str, Any]
    requested_gpu_hours: Decimal
    decided_by: str | None
    decided_at: datetime | None
    submitted_job_id: str | None
    submission_refusal: str | None
    created_at: datetime


@dataclass(frozen=True)
class Decision:
    state: str
    submitted_job_id: str | None = None
    refusal: str | None = None


class OwnerService:
    def __init__(
        self,
        *,
        engine: Engine,
        runs: RunReader,
        authorizer: Authorizer,
        submissions: SubmissionPort,
        auditor: Auditor,
        clock: Clock,
    ) -> None:
        self.engine = engine
        self.runs = runs
        self.authorizer = authorizer
        self.submissions = submissions
        self.auditor = auditor
        self.clock = clock

    # ------------------------------------------------------------ diagnoses

    def diagnoses_for_run(self, principal: Principal, run_id: str) -> list[DiagnosisView]:
        run = self.runs.get_run(run_id)
        if run is None:
            raise NotFound(run_id)
        owner = run.owner_id == principal.principal_id
        if not owner and not self.authorizer.may_read_project(
            principal.principal_id, run.project_id
        ):
            raise NotFound(run_id)
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(diagnoses)
                .where(diagnoses.c.run_id == run_id)
                .order_by(diagnoses.c.created_at)
            ).all()
            findings = {
                f.diagnosis_id: f
                for f in conn.execute(
                    select(hardware_findings).where(hardware_findings.c.run_id == run_id)
                ).all()
            }
        out = []
        for r in rows:
            f = findings.get(r.diagnosis_id)
            out.append(
                DiagnosisView(
                    diagnosis_id=str(r.diagnosis_id),
                    run_id=r.run_id,
                    outcome=r.outcome,
                    classification=r.classification,
                    claims=list(r.claims),
                    matching_earlier_failures=list(r.matching_earlier_failures),
                    stripped_count=r.stripped_count,
                    # The hardware finding goes to the owner; a supervising Professor sees
                    # the narrative only (SADD §8.3).
                    hardware_verdict=f.verdict if f is not None and owner else None,
                    hardware_indicators=list(f.indicators) if f is not None and owner else [],
                    agent_version=r.agent_version,
                    created_at=r.created_at,
                )
            )
        return out

    def recent_diagnoses(self, principal: Principal, limit: int = 100) -> list[DiagnosisSummary]:
        """The diagnoses the caller may open, newest first: their own runs', and a supervising
        Professor's students' (the same rule as diagnoses_for_run). A Platform Admin as such
        sees none (NFR-AUTH-18)."""
        me = principal.principal_id
        with self.engine.connect() as conn:
            projects = conn.execute(
                select(diagnoses.c.project_id, diagnoses.c.owner_id).distinct()
            ).all()
            visible = [
                p.project_id for p in projects
                if p.owner_id == me or self.authorizer.may_read_project(me, p.project_id)
            ]
            if not visible:
                return []
            rows = conn.execute(
                select(diagnoses, proposals.c.state.label("proposal_state"))
                .outerjoin(proposals, proposals.c.diagnosis_id == diagnoses.c.diagnosis_id)
                .where(diagnoses.c.project_id.in_(visible))
                .order_by(diagnoses.c.created_at.desc())
                .limit(limit)
            ).all()
        return [
            DiagnosisSummary(
                diagnosis_id=str(r.diagnosis_id),
                run_id=r.run_id,
                project_id=r.project_id,
                owner_id=r.owner_id,
                mine=r.owner_id == me,
                outcome=r.outcome,
                classification=r.classification,
                proposal_state=r.proposal_state if r.owner_id == me else None,
                created_at=r.created_at,
            )
            for r in rows
        ]

    # ------------------------------------------------------------ proposals

    def _view(self, row: Any) -> ProposalView:
        run = self.runs.get_run(row.run_id)
        return ProposalView(
            proposal_id=str(row.proposal_id),
            run_id=row.run_id,
            diagnosis_id=str(row.diagnosis_id),
            state=row.state,
            changes=list(row.changes),
            current_spec=_spec_dict(run.job_spec) if run else None,
            proposed_spec=dict(row.job_spec),
            requested_gpu_hours=row.requested_gpu_hours,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            submitted_job_id=row.submitted_job_id,
            submission_refusal=row.submission_refusal,
            created_at=row.created_at,
        )

    def list_proposals(self, principal: Principal, state: str | None = None) -> list[ProposalView]:
        query = select(proposals).where(proposals.c.owner_id == principal.principal_id)
        if state:
            query = query.where(proposals.c.state == state)
        with self.engine.connect() as conn:
            rows = conn.execute(query.order_by(proposals.c.created_at.desc())).all()
        return [self._view(r) for r in rows]

    def get_proposal(self, principal: Principal, proposal_id: uuid.UUID) -> ProposalView:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(proposals).where(
                    proposals.c.proposal_id == proposal_id,
                    proposals.c.owner_id == principal.principal_id,
                )
            ).first()
        if row is None:
            raise NotFound(str(proposal_id))
        return self._view(row)

    def approve(self, principal: Principal, proposal_id: uuid.UUID) -> Decision:
        """FR-GPU-149/150: only the owner approves; approval is a commitment operation
        (refused after expiry, ID-03); the job goes through ordinary submission with the
        failed run as parent and the approver recorded. Idempotent per proposal."""
        user = principal.principal_id
        with self.engine.begin() as conn:
            row = conn.execute(
                select(proposals)
                .where(proposals.c.proposal_id == proposal_id, proposals.c.owner_id == user)
                .with_for_update()
            ).first()
            if row is None:
                raise NotFound(str(proposal_id))
            if row.state != "PROPOSED":
                raise Conflict(f"proposal is {row.state}")
            if not self.authorizer.authorization_active(user):
                self.auditor.by(
                    principal,
                    "steward.proposal.approve",
                    str(proposal_id),
                    outcome="denied",
                    reason="authorization inactive",
                )
                raise Forbidden("authorization inactive: approval is a commitment operation")
            if not self.authorizer.may_submit(user, row.project_id):
                self.auditor.by(
                    principal,
                    "steward.proposal.approve",
                    str(proposal_id),
                    outcome="denied",
                    reason="may not submit to project",
                )
                raise Forbidden("you may not submit jobs to this project")
            receipt = self.submissions.submit(
                SubmissionRequest(
                    project_id=row.project_id,
                    job_spec=_spec_from_dict(row.job_spec),
                    parent_run_id=row.run_id,
                    approving_user=user,
                    idempotency_key=str(proposal_id),
                )
            )
            now = self.clock.now()
            if not receipt.accepted or receipt.job_id is None:
                conn.execute(
                    update(proposals)
                    .where(proposals.c.proposal_id == proposal_id)
                    .values(submission_refusal=receipt.reason or "submission refused")
                )
                outcome = Decision("PROPOSED", refusal=receipt.reason or "submission refused")
            else:
                conn.execute(
                    update(proposals)
                    .where(proposals.c.proposal_id == proposal_id)
                    .values(
                        state="APPROVED",
                        decided_by=user,
                        decided_at=now,
                        submitted_job_id=receipt.job_id,
                        submission_refusal=None,
                    )
                )
                outcome = Decision("APPROVED", submitted_job_id=receipt.job_id)
        self.auditor.by(
            principal,
            "steward.proposal.approve",
            str(proposal_id),
            *([outcome.submitted_job_id] if outcome.submitted_job_id else []),
            outcome="ok" if outcome.state == "APPROVED" else "submission_refused",
            reason=outcome.refusal,
        )
        return outcome

    def reject(self, principal: Principal, proposal_id: uuid.UUID) -> Decision:
        """Nothing is queued; the proposal is retained with its outcome (FR-GPU-167)."""
        user = principal.principal_id
        with self.engine.begin() as conn:
            row = conn.execute(
                select(proposals.c.state)
                .where(proposals.c.proposal_id == proposal_id, proposals.c.owner_id == user)
                .with_for_update()
            ).first()
            if row is None:
                raise NotFound(str(proposal_id))
            if row.state != "PROPOSED":
                raise Conflict(f"proposal is {row.state}")
            conn.execute(
                update(proposals)
                .where(proposals.c.proposal_id == proposal_id)
                .values(state="REJECTED", decided_by=user, decided_at=self.clock.now())
            )
        self.auditor.by(principal, "steward.proposal.reject", str(proposal_id))
        return Decision("REJECTED")


def _spec_dict(spec: JobSpec) -> dict[str, Any]:
    from dataclasses import asdict

    d = asdict(spec)
    d["env_pins"] = list(spec.env_pins)
    return d


def _spec_from_dict(d: dict[str, Any]) -> JobSpec:
    return JobSpec(
        command_args=d["command_args"],
        env_pins=tuple(d["env_pins"]),
        gpu_count=int(d["gpu_count"]),
        walltime_minutes=int(d["walltime_minutes"]),
        image_digest=d["image_digest"],
        batch_size=d.get("batch_size"),
        precision=d.get("precision"),
    )
