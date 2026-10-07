"""Composition root: build every Steward service from settings and the platform's ports.

The platform constructs ``Ports`` from its own modules (ADR-S07) and calls ``build``.
Tests and the demo pass fakes. Nothing else in the package constructs services, so
wiring stays in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine

from prayog_steward.audit import Auditor
from prayog_steward.clock import Clock, SystemClock
from prayog_steward.config import StewardSettings
from prayog_steward.coordinator.service import Coordinator
from prayog_steward.db.engine import make_engine
from prayog_steward.gateway import Gateway
from prayog_steward.owner import OwnerService
from prayog_steward.ports import (
    AllocationHistory,
    AuditSink,
    Authorizer,
    LedgerHistory,
    LogStore,
    PrincipalResolver,
    RebuildAttempts,
    RunReader,
    SubmissionPort,
)
from prayog_steward.rag.embed import Embedder, build_embedder
from prayog_steward.rag.sanitiser import Sanitiser
from prayog_steward.reasoning_client import HttpReasoning
from prayog_steward.search import WebSearch, build_search
from prayog_steward.sessions.context import AttemptContext
from prayog_steward.sessions.links import LinkService
from prayog_steward.sessions.s3 import S3Service
from prayog_steward.sessions.s4 import OptimizeService
from steward_contracts import ReasoningPort


@dataclass
class Ports:
    """Everything the Steward needs from other sub-teams."""

    runs: RunReader
    logs: LogStore
    principals: PrincipalResolver
    authorizer: Authorizer
    attempts: RebuildAttempts
    submissions: SubmissionPort
    audit: AuditSink
    allocations: AllocationHistory
    ledger: LedgerHistory


@dataclass
class Steward:
    settings: StewardSettings
    ports: Ports
    engine: Engine
    clock: Clock
    reasoning: ReasoningPort
    auditor: Auditor
    sanitiser: Sanitiser
    embedder: Embedder
    search: WebSearch
    gateway: Gateway
    coordinator: Coordinator
    owner: OwnerService
    links: LinkService
    s3: S3Service
    s4: OptimizeService
    maintenance_engine: Engine | None = field(default=None)


def build(
    settings: StewardSettings,
    ports: Ports,
    *,
    engine: Engine | None = None,
    maintenance_engine: Engine | None = None,
    reasoning: ReasoningPort | None = None,
    clock: Clock | None = None,
    embedder: Embedder | None = None,
    search: WebSearch | None = None,
) -> Steward:
    engine = engine or make_engine(settings.database_url)
    if maintenance_engine is None and settings.maintenance_database_url:
        maintenance_engine = make_engine(settings.maintenance_database_url)
    clock = clock or SystemClock()
    reasoning = reasoning or HttpReasoning(settings.runtime_url, settings.runtime_timeout_seconds)
    auditor = Auditor(ports.audit, clock, settings.service_principal_id)
    sanitiser = Sanitiser()
    embedder = embedder or build_embedder(settings.embedder)
    search = search or build_search(
        settings.search_provider,
        settings.search_api_key,
        settings.search_timeout_seconds,
        settings.search_max_results,
    )
    gateway = Gateway(
        runs=ports.runs, logs=ports.logs, sanitiser=sanitiser, settings=settings, auditor=auditor
    )
    context = AttemptContext(
        gateway=gateway,
        embedder=embedder,
        runs=ports.runs,
        ledger=ports.ledger,
        settings=settings,
        clock=clock,
    )
    return Steward(
        settings=settings,
        ports=ports,
        engine=engine,
        clock=clock,
        reasoning=reasoning,
        auditor=auditor,
        sanitiser=sanitiser,
        embedder=embedder,
        search=search,
        gateway=gateway,
        coordinator=Coordinator(
            engine=engine,
            settings=settings,
            clock=clock,
            reasoning=reasoning,
            gateway=gateway,
            auditor=auditor,
            runs=ports.runs,
        ),
        owner=OwnerService(
            engine=engine,
            runs=ports.runs,
            authorizer=ports.authorizer,
            submissions=ports.submissions,
            auditor=auditor,
            clock=clock,
        ),
        links=LinkService(
            engine=engine,
            settings=settings,
            clock=clock,
            attempts=ports.attempts,
            authorizer=ports.authorizer,
            auditor=auditor,
        ),
        s3=S3Service(
            engine=engine,
            settings=settings,
            clock=clock,
            attempts=ports.attempts,
            runs=ports.runs,
            context=context,
            gateway=gateway,
            sanitiser=sanitiser,
            search=search,
            reasoning=reasoning,
            auditor=auditor,
        ),
        s4=OptimizeService(
            attempts=ports.attempts,
            runs=ports.runs,
            ledger=ports.ledger,
            gateway=gateway,
            reasoning=reasoning,
            auditor=auditor,
            settings=settings,
        ),
        maintenance_engine=maintenance_engine,
    )
