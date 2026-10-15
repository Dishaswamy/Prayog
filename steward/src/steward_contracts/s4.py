"""S4 — optimisation advisory after REBUILD_SUCCESS (FR-STW-006)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from steward_contracts.base import Context, Frozen, Record


class SuggestionKind(StrEnum):
    ARCHITECTURE = "architecture"
    HYPERPARAMETER = "hyperparameter"
    TRAINING_SETUP = "training_setup"


class Suggestion(Frozen):
    kind: SuggestionKind
    text: str
    cites: list[str]


class S4Input(Frozen):
    context: Literal[Context.S4] = Context.S4
    attempt_id: str
    records: list[Record]


class S4Result(Frozen):
    """Strictly advisory; nothing is applied or executed (UC-STW-03, A08)."""

    suggestions: list[Suggestion]
