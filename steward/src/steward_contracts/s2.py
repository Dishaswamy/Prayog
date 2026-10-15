"""S2 — fault correlation over a metadata-only evidence bundle (FR-GPU-162…166)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from steward_contracts.base import Context, Frozen
from steward_contracts.s1 import Claim, HardwareFinding


class EvidenceRow(Frozen):
    """A fault-evidence row as handed to S2. Metadata only: no log bodies, metric values,
    code, configuration or project names (FR-GPU-159, NFR-GPU-056)."""

    row_id: str
    node_id: str
    unit_id: str
    owner_lab: str
    at: datetime
    exit_code: int | None
    failure_category: str
    failure_signature: str | None = None  # ADR-S05, pending CR-S1


class S2Input(Frozen):
    context: Literal[Context.S2] = Context.S2
    warning_id: str
    rows: list[EvidenceRow]


class S2Result(Frozen):
    """Advisory only. The server's threshold raised the warning (SADD ADR-21)."""

    same_fault: bool
    row_ids: list[str]
    finding: HardwareFinding
    rationale: list[Claim]  # must cite row IDs from the bundle
