"""Steward configuration (Integrated Spec §16.2, §16.4) and its invariants (§16.7)."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StewardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STEWARD_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    database_url: str = "postgresql+psycopg://steward:steward@localhost:5432/prayog"
    # Optional role WITH BYPASSRLS, used only by the retention purge and aggregate health
    # counts (NFR-SEC-008, FR-ML-005). Never used on a request path.
    maintenance_database_url: str | None = None
    runtime_url: str = "http://localhost:8100"
    runtime_timeout_seconds: float = 120.0
    public_base_url: str = "http://localhost:8000"  # where Steward links point (FR-STW-004)
    service_principal_id: str = "svc:steward"  # FR-AUTH-73
    # Where the Steward page signs users in when the platform uses bearer tokens
    # (POST {email, password} -> {access_token}). None: the platform's session cookie.
    platform_login_url: str | None = None
    # Job fields the platform's submission path can actually change. A proposal may only
    # touch these (the model is told; anything else is dropped). None: all JobField values.
    proposal_fields: list[str] | None = None
    # Secure flag on the S3 session cookie. None = follow the request scheme; set True
    # behind a TLS-terminating proxy, where the app itself sees plain http.
    cookie_secure: bool | None = None

    # §16.2 Fault correlation
    correlation_window: timedelta = timedelta(hours=24)  # needs calibration (ADR-S08)
    min_distinct_users_for_warning: int = 2
    warning_cooldown: timedelta = timedelta(hours=24)
    max_evidence_bundle_rows: int = 50

    # §16.4 Steward and ledger
    steward_link_ttl: timedelta = timedelta(minutes=15)
    rag_retention_period: timedelta = timedelta(days=90)
    s3_session_ttl: timedelta = timedelta(minutes=60)  # interactive session after the link

    # RAG pipeline (ADR-S06, ADR-S09)
    embedder: str = "hashing"  # or "st:BAAI/bge-small-en-v1.5" with the [rag] extra
    rag_top_k: int = 6
    rag_max_candidates: int = 2_000
    rag_chunk_chars: int = 800
    ledger_entries_limit: int = 50
    s3_history_turns: int = 6
    search_api_key: str | None = None

    # Knowledge base of reference papers (ADR-S10). "auto" = BGE-base when
    # sentence-transformers is installed, else hashing. Ingest and query must match.
    knowledge_enabled: bool = True
    knowledge_embedder: str = "auto"
    knowledge_top_k: int = 3
    # Relevance floor; None = calibrated per embedder (KnowledgeIndex.default_min_score).
    knowledge_min_score: float | None = None
    search_timeout_seconds: float = 8.0
    search_max_results: int = 3

    # Web search (ADR-S06). The model, effort and API key are the Runtime's settings,
    # read by that process (steward_runtime.app), never by the server.
    search_provider: Literal["none", "tavily", "brave"] = "none"

    # Coordinator retries (NFR-GPU-055: the Steward being down never blocks anything)
    task_max_attempts: int = 5
    task_backoff_base: timedelta = timedelta(seconds=30)
    task_lease: timedelta = timedelta(minutes=5)  # a crashed worker's task is retried after this
    worker_poll_seconds: float = 2.0

    # Proposal bounds the server enforces whatever the model says (ADR-S04)
    max_gpu_count: int = 8
    max_walltime_minutes: int = 48 * 60  # L4: guests may hold a node for at most 48 h

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
        if self.s3_session_ttl <= timedelta(0) or self.task_lease <= timedelta(0):
            raise ValueError("s3_session_ttl and task_lease must be > 0")
        return self
