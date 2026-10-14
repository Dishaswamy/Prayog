"""S3: interactive rebuild diagnosis (UC-STW-02, FR-STW-005, 010-018).

One question per call. Validate the session, then retrieve isolated context (one private
transaction), optionally search the web with an allow-listed query, call the model with
no transaction open, validate the answer server-side, then store the exchange (another
private transaction). The answer is returned as structured data. Commands are text for
the user to run themselves, never executed (FR-STW-013, 014). If the model is down the
attempt stays REBUILD_FAILED and the user gets a clear message (FR-STW-010).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from prayog_steward.audit import Auditor
from prayog_steward.clock import Clock
from prayog_steward.config import StewardSettings
from prayog_steward.coordinator.validation import validate_response
from prayog_steward.db.engine import private_tx
from prayog_steward.db.models import response_cache, s3_messages
from prayog_steward.gateway import Gateway
from prayog_steward.knowledge.index import KnowledgeIndex, reference_records
from prayog_steward.ports import Principal, RebuildAttempts, RebuildState, RunReader
from prayog_steward.rag.sanitiser import Sanitiser, extract_error_facts
from prayog_steward.search import SearchState, WebSearch
from prayog_steward.sessions.context import AttemptContext
from prayog_steward.sessions.links import AccessDenied, ActiveSession, Denial
from steward_contracts import (
    InvokeOutcome,
    InvokeRequest,
    ReasoningPort,
    Record,
    RecordKind,
    S3Exchange,
    S3Input,
    S3Turn,
    SearchStatus,
)

NO_EVIDENCE_NOTICE = (
    "No matching evidence was found in your own ledger for this attempt. The answer below "
    "is general reasoning over the error, not a finding from your records."
)
UNAVAILABLE_NOTICE = (
    "Steward is temporarily unavailable. Your rebuild attempt is still REBUILD_FAILED and "
    "nothing was changed. Please try again shortly."
)
MAX_QUESTION_CHARS = 2_000


@dataclass(frozen=True)
class S3Answer:
    outcome: str  # ok | unavailable | refused | malformed
    attempt_id: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    evidence_found: bool = False
    search: str = SearchState.DISABLED.value
    sources: dict[str, dict[str, str | None]] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    cached: bool = False


class S3Service:
    def __init__(
        self,
        *,
        engine: Engine,
        settings: StewardSettings,
        clock: Clock,
        attempts: RebuildAttempts,
        runs: RunReader,
        context: AttemptContext,
        gateway: Gateway,
        sanitiser: Sanitiser,
        search: WebSearch,
        knowledge: KnowledgeIndex | None = None,
        reasoning: ReasoningPort,
        auditor: Auditor,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.clock = clock
        self.attempts = attempts
        self.runs = runs
        self.context = context
        self.gateway = gateway
        self.sanitiser = sanitiser
        self.search = search
        self.knowledge = knowledge
        self.reasoning = reasoning
        self.auditor = auditor

    def open(self, principal: Principal, session: ActiveSession) -> int:
        """Ingest the attempt's sanitised context once, right after the link is consumed."""
        attempt = self._owned_failed(principal, session.attempt_id)
        with private_tx(self.engine, principal.principal_id) as conn:
            return self.context.ingest(conn, principal.principal_id, attempt)

    def history(self, principal: Principal, session: ActiveSession) -> list[dict[str, Any]]:
        with private_tx(self.engine, principal.principal_id) as conn:
            rows = conn.execute(
                select(s3_messages.c.role, s3_messages.c.content, s3_messages.c.created_at)
                .where(s3_messages.c.attempt_id == session.attempt_id)
                .order_by(s3_messages.c.created_at)
            ).all()
        return [
            {"role": r.role, "content": r.content, "at": r.created_at.isoformat()} for r in rows
        ]

    def _owned_failed(self, principal: Principal, attempt_id: str) -> Any:
        attempt = self.attempts.get(attempt_id)
        if attempt is None or attempt.owner_id != principal.principal_id:
            self.auditor.by(
                principal, "steward.rag.retrieve", attempt_id, outcome="denied", reason="not owner"
            )
            raise AccessDenied(Denial.NOT_FOUND, "Access denied.")
        if attempt.state is not RebuildState.FAILED:
            raise AccessDenied(
                Denial.WRONG_STATE,
                "Steward diagnosis is available only for REBUILD_FAILED attempts.",
            )
        return attempt

    def ask(
        self,
        principal: Principal,
        session: ActiveSession,
        question: str,
        include_attempts: list[str] | None = None,
    ) -> S3Answer:
        attempt = self._owned_failed(principal, session.attempt_id)
        extra: list[str] = []
        for other_id in include_attempts or []:
            other = self.attempts.get(other_id)
            if other is None or other.owner_id != principal.principal_id:  # A13
                self.auditor.by(
                    principal,
                    "steward.rag.retrieve",
                    other_id,
                    outcome="denied",
                    reason="attempt not owned",
                )
                raise AccessDenied(Denial.NOT_FOUND, f"No rebuild attempt {other_id} of yours.")
            if other_id != attempt.attempt_id:
                extra.append(other_id)
        attempt_ids = [attempt.attempt_id, *extra]
        question = self.sanitiser.sanitise(question[:MAX_QUESTION_CHARS]).text.strip()
        if not question:
            raise ValueError("question is empty")

        user = principal.principal_id
        with private_tx(self.engine, user) as conn:
            self.context.ingest(conn, user, attempt)  # no-op once ingested
            for other_id in extra:
                other = self.attempts.get(other_id)
                if other is not None:
                    self.context.ingest(conn, user, other)
            history = self._recent_history(conn, attempt.attempt_id)
            key = self._cache_key(user, attempt_ids, question)
            cached = conn.execute(
                select(response_cache.c.response).where(response_cache.c.cache_key == key)
            ).scalar()
            if cached is not None:
                return S3Answer(**{**cached, "cached": True})
            records = self.context.retrieve(conn, attempt_ids, question, self.settings.rag_top_k)
            error_text = self.context.error_text(conn, attempt.attempt_id)

        run = self.runs.get_run(attempt.run_id) if attempt.run_id else None
        facts = extract_error_facts(error_text, run.job_spec.env_pins if run else ())
        found = self.search.lookup(facts)
        # Literature: local retrieval over the paper corpus, by question and error class.
        references, ref_titles = reference_records(
            self.knowledge,
            f"{question} {facts.exception_class or ''}".strip(),
            self.settings.knowledge_top_k,
            self.settings.knowledge_min_score,
        )
        external = [
            Record(
                record_id=f"web:{i}",
                kind=RecordKind.EXTERNAL,
                body=self.sanitiser.sanitise(f"{r.title}\n{r.snippet}"[:2_000]).text,
                source_url=r.url,
            )
            for i, r in enumerate(found.results)
        ]
        request = InvokeRequest(
            invocation_id=str(uuid.uuid4()),
            payload=S3Input(
                attempt_id=attempt.attempt_id,
                question=question,
                records=records + references + external,
                history=history,
                search_status=SearchStatus(found.state.value),
            ),
        )
        response = self.reasoning.invoke(request)
        validated = validate_response(request, response)
        if validated.out_of_contract:
            self.auditor.by_steward(
                "steward.write.refused",
                request.invocation_id,
                outcome="refused",
                reason=validated.problem,
            )
        if not isinstance(validated.result, S3Turn):
            notice = (
                UNAVAILABLE_NOTICE
                if validated.outcome is InvokeOutcome.UNAVAILABLE
                else "Steward could not produce a usable answer. Please rephrase or retry."
            )
            self.auditor.by(
                principal, "steward.s3.ask", attempt.attempt_id, outcome=validated.outcome.value
            )
            return S3Answer(
                validated.outcome.value,
                attempt.attempt_id,
                notices=[notice],
                search=found.state.value,
            )

        answer = self._answer(
            attempt.attempt_id, validated.result, request, found.state, ref_titles
        )
        with private_tx(self.engine, user) as conn:
            now = self.clock.now()
            conn.execute(
                s3_messages.insert(),
                [
                    {
                        "message_id": uuid.uuid4(),
                        "user_id": user,
                        "attempt_id": attempt.attempt_id,
                        "role": "user",
                        "content": {"question": question},
                        "created_at": now,
                    },
                    {
                        "message_id": uuid.uuid4(),
                        "user_id": user,
                        "attempt_id": attempt.attempt_id,
                        "role": "steward",
                        "content": json.loads(json.dumps(answer.__dict__, default=str)),
                        "created_at": now,
                    },
                ],
            )
            conn.execute(
                pg_insert(response_cache)
                .values(
                    cache_key=key,
                    user_id=user,
                    attempt_id=attempt.attempt_id,
                    response=json.loads(json.dumps(answer.__dict__, default=str)),
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["cache_key"])  # a concurrent twin
            )
        self.auditor.by(
            principal,
            "steward.s3.ask",
            attempt.attempt_id,
            reason=f"stripped={validated.stripped}" if validated.stripped else None,
        )
        return answer

    def _answer(
        self,
        attempt_id: str,
        turn: S3Turn,
        request: InvokeRequest,
        search: SearchState,
        ref_titles: dict[str, str],
    ) -> S3Answer:
        payload = request.payload
        assert isinstance(payload, S3Input)
        by_id = {r.record_id: r for r in payload.records}
        used = {c for claim in turn.claims for c in claim.cites}
        notices = []
        if not turn.evidence_found:
            notices.append(NO_EVIDENCE_NOTICE)
        if search is SearchState.UNAVAILABLE:
            notices.append("Web search was unavailable; the answer uses your local context only.")
        if any(c.provenance.value == "external" for c in turn.claims):
            notices.append(
                "Statements marked external come from the web and are not guaranteed to be correct."
            )
        if any(c.provenance.value == "literature" for c in turn.claims):
            notices.append(
                "Statements marked literature summarise published papers; they are general "
                "findings, not measurements of your run."
            )
        return S3Answer(
            outcome="ok",
            attempt_id=attempt_id,
            claims=[c.model_dump(mode="json") for c in turn.claims],
            steps=[s.model_dump(mode="json") for s in turn.steps],
            evidence_found=turn.evidence_found,
            search=search.value,
            sources={
                rid: {
                    "kind": by_id[rid].kind.value,
                    "url": by_id[rid].source_url,
                    "title": ref_titles.get(rid),
                }
                for rid in sorted(used)
                if rid in by_id
            },
            notices=notices,
        )

    def _recent_history(self, conn: Any, attempt_id: str) -> list[S3Exchange]:
        rows = conn.execute(
            select(s3_messages.c.role, s3_messages.c.content)
            .where(s3_messages.c.attempt_id == attempt_id)
            .order_by(s3_messages.c.created_at.desc(), s3_messages.c.role)
            .limit(self.settings.s3_history_turns * 2)
        ).all()
        rows = list(reversed(rows))
        exchanges: list[S3Exchange] = []
        pending: str | None = None
        for r in rows:
            if r.role == "user":
                pending = r.content.get("question", "")
            elif pending is not None:
                summary = " ".join(c.get("text", "") for c in r.content.get("claims", []))
                exchanges.append(S3Exchange(question=pending, answer=summary[:2_000]))
                pending = None
        return exchanges

    @staticmethod
    def _cache_key(user: str, attempt_ids: list[str], question: str) -> str:
        """Partitioned by user and attempt(s) (NFR-SEC-004): a cached answer can only ever
        be served back to the same user for the same attempts."""
        material = json.dumps([user, sorted(attempt_ids), " ".join(question.lower().split())])
        return hashlib.sha256(material.encode()).hexdigest()
