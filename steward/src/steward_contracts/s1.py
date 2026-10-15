"""S1 — run-failure diagnosis (FR-GPU-146…151, 160, 161, 166, 167, 187)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from steward_contracts.base import Context, Frozen, Record


class Claim(Frozen):
    """A statement and the records it rests on. Uncited claims are stripped (FR-GPU-161)."""

    text: str
    cites: list[str]


class FailureClass(StrEnum):
    """Placeholder label set from the Project Document §10 (ADR-S08: open)."""

    OOM = "oom"
    MISSING_DATA = "missing_data"
    LIBRARY_VERSION = "library_version"
    CODE_BUG = "code_bug"
    INTERRUPTION = "interruption"
    HARDWARE = "hardware"
    UNKNOWN = "unknown"


class HardwareVerdict(StrEnum):
    LIKELY_HARDWARE = "likely_hardware"
    UNLIKELY_HARDWARE = "unlikely_hardware"
    INCONCLUSIVE = "inconclusive"


class HardwareIndicator(StrEnum):
    ECC_ERRORS = "ecc_errors"
    XID_ERROR = "xid_error"
    DEVICE_LOST = "device_lost"
    NCCL_TIMEOUT = "nccl_timeout"
    THERMAL_THROTTLING = "thermal_throttling"
    MEMORY_BELOW_EXPECTED = "memory_below_expected"
    REPEATED_ON_SAME_UNIT = "repeated_on_same_unit"


class HardwareFinding(Frozen):
    """Admin-facing finding. Enums only, no free text, so no project content can leak
    to the Platform Admin (FR-GPU-166, NFR-AUTH-18, ADR-S04)."""

    verdict: HardwareVerdict
    indicators: list[HardwareIndicator]


class JobField(StrEnum):
    """Job fields a proposal may change (ADR-S08: open)."""

    ENV_PIN = "env_pin"  # e.g. "torch==2.3.1"
    COMMAND_ARGS = "command_args"
    GPU_COUNT = "gpu_count"
    WALLTIME_MINUTES = "walltime_minutes"
    BATCH_SIZE = "batch_size"
    PRECISION = "precision"


class JobChange(Frozen):
    field: JobField
    new_value: str
    reason: Claim


class ProposalDraft(Frozen):
    """The model's suggestion only. The server computes the exact diff and GPU-hours
    shown to the owner (FR-GPU-187), and nothing is queued without approval (FR-GPU-149)."""

    changes: list[JobChange]


class S1Input(Frozen):
    context: Literal[Context.S1] = Context.S1
    run_id: str
    records: list[Record]


class S1Result(Frozen):
    classification: FailureClass
    claims: list[Claim]
    matching_earlier_failures: list[str]  # record IDs of EARLIER_RUN records
    hardware_finding: HardwareFinding
    proposal: ProposalDraft | None
