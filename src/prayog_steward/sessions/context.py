"""RAG context for S3: ingestion and retrieval, isolated per user and per attempt.

Ingestion (on opening a link) and retrieval (per question) both run inside ``private_tx``,
so row-level security filters every statement to the authenticated user before anything
reaches prompt assembly (FR-STW-015, NFR-SEC-002). Rows carry ``attempt_id``, and
retrieval is limited to the session's attempt plus any earlier attempts the user names
explicitly and owns (A13, NFR-SEC-004). Everything stored has been sanitised (FR-STW-011).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from prayog_steward.clock import Clock
from prayog_steward.config import StewardSettings
from prayog_steward.db.models import diagnoses, rag_chunks
from prayog_steward.gateway import Gateway
from prayog_steward.ports import AttemptView, LedgerHistory, RunReader
from prayog_steward.rag.embed import Embedder, chunk, cosine
from steward_contracts import Record, RecordKind


@dataclass(frozen=True)
class Source:
    ref: str
    kind: RecordKind
    text: str


class AttemptContext:
    def __init__(
        self,
        *,
        gateway: Gateway,
        embedder: Embedder,
        runs: RunReader,
        ledger: LedgerHistory,
        settings: StewardSettings,
        clock: Clock,
    ) -> None:
        self.gateway = gateway
        self.embedder = embedder
        self.runs = runs
        self.ledger = ledger
        self.settings = settings
        self.clock = clock

    # ------------------------------------------------------------ sources

    def sources(self, conn: Connection, attempt: AttemptView) -> list[Source]:
        """Sanitised material for one failed attempt: its run record, error logs, the S1
        diagnosis of the same run (ADR-S08) and the owner's ledger entries for the project."""
        out: list[Source] = []
        run = self.runs.get_run(attempt.run_id) if attempt.run_id else None
        if run is not None and run.owner_id == attempt.owner_id:
            record = self.gateway.run_record(run)
            out.append(Source(f"run:{run.run_id}", RecordKind.RUN, record.body))
            for log_record in self.gateway.log_records(run):
                out.append(Source(log_record.record_id, RecordKind.LOG_EXCERPT, log_record.body))
            diag = conn.execute(
                select(diagnoses.c.diagnosis_id, diagnoses.c.classification, diagnoses.c.claims)
                .where(diagnoses.c.run_id == run.run_id, diagnoses.c.outcome == "ok")
                .order_by(diagnoses.c.created_at.desc())
                .limit(1)
            ).first()
            if diag is not None:
                text = json.dumps(
                    {
                        "classification": diag.classification,
                        "claims": [c["text"] for c in diag.claims],
                    }
                )
                out.append(Source(f"diagnosis:{diag.diagnosis_id}", RecordKind.DIAGNOSIS, text))
        for entry in self.ledger.for_user(
            attempt.owner_id, attempt.project_id, self.settings.ledger_entries_limit
        ):
            if entry.owner_id != attempt.owner_id:  # defence in depth (NFR-SEC-002)
                continue
            body = self.gateway.record(f"led:{entry.entry_id}", RecordKind.LEDGER, entry.body).body
            out.append(Source(f"led:{entry.entry_id}", RecordKind.LEDGER, f"[{entry.kind}] {body}"))
        return out

    # ------------------------------------------------------------ ingestion

    def ingest(self, conn: Connection, user_id: str, attempt: AttemptView) -> int:
        """Idempotent: re-ingesting the same attempt inserts nothing new."""
        rows = []
        now = self.clock.now()
        for source in self.sources(conn, attempt):
            pieces = chunk(source.text, self.settings.rag_chunk_chars)
            if not pieces:
                continue
            for seq, (piece, vector) in enumerate(
                zip(pieces, self.embedder.embed(pieces), strict=True)
            ):
                rows.append(
                    {
                        "chunk_id": uuid.uuid4(),
                        "user_id": user_id,
                        "attempt_id": attempt.attempt_id,
                        "source_ref": source.ref,
                        "seq": seq,
                        "kind": source.kind.value,
                        "body": piece,
                        "embedder": self.embedder.name,
                        "embedding": vector,
                        "created_at": now,
                    }
                )
        if not rows:
            return 0
        result = conn.execute(
            insert(rag_chunks)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["user_id", "attempt_id", "source_ref", "seq"])
            .returning(rag_chunks.c.chunk_id)
        )
        return len(result.all())

    # ------------------------------------------------------------ retrieval

    def retrieve(
        self,
        conn: Connection,
        attempt_ids: Sequence[str],
        query: str,
        k: int,
        pinned_log_chunks: int = 4,
    ) -> list[Record]:
        """The primary attempt's own error log (always relevant, FR-STW-011) plus the top-k
        other chunks by cosine similarity, among this user's chunks for these attempts.
        RLS has already removed every other user's rows, and the attempt filter removes
        unrelated attempts (NFR-SEC-004)."""
        if not attempt_ids:
            return []
        candidates = conn.execute(
            select(
                rag_chunks.c.source_ref,
                rag_chunks.c.seq,
                rag_chunks.c.kind,
                rag_chunks.c.body,
                rag_chunks.c.embedding,
                rag_chunks.c.attempt_id,
            )
            .where(
                rag_chunks.c.attempt_id.in_(list(attempt_ids)),
                rag_chunks.c.embedder == self.embedder.name,
            )
            .order_by(rag_chunks.c.source_ref, rag_chunks.c.seq)
            .limit(self.settings.rag_max_candidates)
        ).all()
        if not candidates:
            return []
        primary = attempt_ids[0]
        pinned = [
            r
            for r in candidates
            if r.attempt_id == primary and r.kind == RecordKind.LOG_EXCERPT.value
        ][-pinned_log_chunks:]  # the tail of the log is where the error is
        pinned_keys = {(r.source_ref, r.seq) for r in pinned}
        rest = [r for r in candidates if (r.source_ref, r.seq) not in pinned_keys]
        (qvec,) = self.embedder.embed([query])
        ranked = sorted(rest, key=lambda r: cosine(qvec, r.embedding), reverse=True)[:k]
        multi = len(attempt_ids) > 1
        return [
            Record(
                record_id=f"{r.source_ref}#{r.seq}",
                kind=RecordKind(r.kind),
                body=f"(attempt {r.attempt_id}) {r.body}" if multi else r.body,
            )
            for r in [*pinned, *ranked]
        ]

    def error_text(self, conn: Connection, attempt_id: str) -> str:
        """The attempt's sanitised error log, for extracting search facts."""
        rows = conn.execute(
            select(rag_chunks.c.body)
            .where(
                rag_chunks.c.attempt_id == attempt_id,
                rag_chunks.c.kind == RecordKind.LOG_EXCERPT.value,
            )
            .order_by(rag_chunks.c.source_ref, rag_chunks.c.seq)
        ).all()
        return "\n".join(r.body for r in rows)
