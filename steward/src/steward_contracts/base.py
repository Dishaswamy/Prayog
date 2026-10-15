"""Building blocks shared by every context."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Frozen(BaseModel):
    """Immutable model that rejects unknown fields (ADR-S04)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Context(StrEnum):
    """The four invocation contexts of the one Steward subsystem (Integrated Spec ID-05)."""

    S1 = "S1"  # run-failure diagnosis
    S2 = "S2"  # fault correlation
    S3 = "S3"  # interactive rebuild diagnosis
    S4 = "S4"  # optimisation advisory


class RecordKind(StrEnum):
    RUN = "run"  # scheduling data of the invoked run (FR-GPU-148)
    TELEMETRY = "telemetry"
    LOG_EXCERPT = "log_excerpt"  # sanitised (FR-STW-011, ADR-S03)
    EARLIER_RUN = "earlier_run"  # same project only (FR-GPU-148)
    DIAGNOSIS = "diagnosis"  # an earlier Steward diagnosis of the same owner
    LEDGER = "ledger"  # retrieved ledger evidence (S3, S4)
    METRICS = "metrics"  # permitted metrics of a run (S4, ADR-S08)
    CONFIG = "config"  # hyperparameters / job configuration
    EXTERNAL = "external"  # web search result (FR-STW-017)
    REFERENCE = "reference"  # passage from the curated paper corpus (ADR-S10)


class Record(Frozen):
    """One piece of sanitised context handed to the model.

    ``record_id`` is what claims cite. The server only accepts citations of IDs it
    handed out in the same invocation (FR-GPU-161).
    """

    record_id: str
    kind: RecordKind
    body: str
    source_url: str | None = None  # set for EXTERNAL and REFERENCE records
