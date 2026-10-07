"""Steward configuration (Integrated Spec §16.2, §16.4) and its invariants (§16.7)."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StewardSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STEWARD_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://steward:steward@localhost:5432/prayog"
    runtime_url: str = "http://localhost:8100"

    # §16.2 Fault correlation
    correlation_window: timedelta = timedelta(hours=24)  # needs calibration (ADR-S08)
    min_distinct_users_for_warning: int = 2
    warning_cooldown: timedelta = timedelta(hours=24)
    max_evidence_bundle_rows: int = 50

    # §16.4 Steward and ledger
    steward_link_ttl: timedelta = timedelta(minutes=15)
    rag_retention_period: timedelta = timedelta(days=90)

    # Reasoning (ADR-S02) and search (ADR-S06)
    reasoning: Literal["stub", "claude"] = "stub"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    search_provider: Literal["none", "tavily", "brave"] = "none"

    # Coordinator retries (NFR-GPU-055: the Steward being down never blocks anything)
    task_max_attempts: int = 5
    task_backoff_base: timedelta = timedelta(seconds=30)

    # S1 input bounds: keep the prompt small and the bundle predictable
    earlier_runs_limit: int = 10
    log_excerpt_max_chars: int = 12_000

    @model_validator(mode="after")
    def _invariants(self) -> StewardSettings:
        # §16.7: a zero TTL could never be opened; a zero retention would purge mid-diagnosis.
        if self.steward_link_ttl <= timedelta(0):
            raise ValueError("steward_link_ttl must be > 0")
        if self.rag_retention_period <= timedelta(0):
            raise ValueError("rag_retention_period must be > 0")
        if self.min_distinct_users_for_warning < 2:
            # The "two distinct users" rule is what stops a healthy machine being accused
            # because one user's code is broken (UC-GPU-21).
            raise ValueError("min_distinct_users_for_warning must be >= 2")
        if self.correlation_window <= timedelta(0) or self.warning_cooldown < timedelta(0):
            raise ValueError("correlation_window must be > 0 and warning_cooldown >= 0")
        if self.max_evidence_bundle_rows < self.min_distinct_users_for_warning:
            raise ValueError("max_evidence_bundle_rows must hold at least the threshold rows")
        return self
