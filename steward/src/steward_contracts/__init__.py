"""Contracts shared by the Steward server and the Steward Runtime.

This package is a leaf: it imports neither ``prayog_steward`` nor ``steward_runtime``.
Every model forbids unknown fields (ADR-S04), so a Runtime response that tries to
smuggle in an action is rejected as malformed rather than silently ignored.
"""

from steward_contracts.base import Context, Frozen, Record, RecordKind
from steward_contracts.envelope import (
    RESULT_TYPES,
    InvokeOutcome,
    InvokeRequest,
    InvokeResponse,
    ReasoningPort,
    StewardInput,
    StewardResult,
)
from steward_contracts.s1 import (
    Claim,
    FailureClass,
    HardwareFinding,
    HardwareIndicator,
    HardwareVerdict,
    JobChange,
    JobField,
    ProposalDraft,
    S1Input,
    S1Result,
)
from steward_contracts.s2 import EvidenceRow, S2Input, S2Result
from steward_contracts.s3 import (
    Provenance,
    RemediationStep,
    S3Exchange,
    S3Input,
    S3Turn,
    SearchStatus,
    SourcedClaim,
)
from steward_contracts.s4 import S4Input, S4Result, Suggestion, SuggestionKind

__all__ = [
    "RESULT_TYPES",
    "Claim",
    "Context",
    "EvidenceRow",
    "FailureClass",
    "Frozen",
    "HardwareFinding",
    "HardwareIndicator",
    "HardwareVerdict",
    "InvokeOutcome",
    "InvokeRequest",
    "InvokeResponse",
    "JobChange",
    "JobField",
    "ProposalDraft",
    "Provenance",
    "ReasoningPort",
    "Record",
    "RecordKind",
    "RemediationStep",
    "S1Input",
    "S1Result",
    "S2Input",
    "S2Result",
    "S3Exchange",
    "S3Input",
    "S3Turn",
    "S4Input",
    "S4Result",
    "SearchStatus",
    "SourcedClaim",
    "StewardInput",
    "StewardResult",
    "Suggestion",
    "SuggestionKind",
]
