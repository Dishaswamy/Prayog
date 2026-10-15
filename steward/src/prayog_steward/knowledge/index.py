"""In-memory search over the knowledge base (ADR-S10).

All chunks for the active embedder are loaded into one normalised float32 matrix and
searched by a single matrix-vector product. At the corpus sizes a course platform needs
(hundreds to tens of thousands of chunks) that takes milliseconds, with no vector
database to run. The matrix reloads when the corpus changes, checked at most every
``refresh_seconds``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import Engine, func, select

from prayog_steward.db.models import knowledge_chunks, knowledge_documents
from prayog_steward.rag.embed import Embedder
from steward_contracts import Record, RecordKind


@dataclass(frozen=True)
class KnowledgeHit:
    record_id: str  # e.g. 'ref:arxiv-1706.02677#4'; what claims cite
    source: str
    title: str
    url: str | None
    seq: int
    text: str
    score: float


def _slug(source: str) -> str:
    return source.replace(":", "-")


class KnowledgeIndex:
    def __init__(self, engine: Engine, embedder: Embedder, refresh_seconds: float = 60.0) -> None:
        self.engine = engine
        self.embedder = embedder
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._version: tuple[int, Any] | None = None
        self._checked_at = 0.0
        self._matrix: np.ndarray[Any, np.dtype[np.float32]] = np.zeros((0, 1), np.float32)
        self._meta: list[tuple[str, str, str | None, int, str]] = []

    def _current_version(self) -> tuple[int, Any]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(
                    func.count(knowledge_chunks.c.chunk_id),
                    func.max(knowledge_documents.c.ingested_at),
                )
                .select_from(
                    knowledge_chunks.join(
                        knowledge_documents,
                        knowledge_documents.c.doc_id == knowledge_chunks.c.doc_id,
                    )
                )
                .where(knowledge_chunks.c.embedder == self.embedder.name)
            ).one()
        return int(row[0]), row[1]

    def _refresh(self) -> None:
        now = time.monotonic()
        if self._version is not None and now - self._checked_at < self.refresh_seconds:
            return
        self._checked_at = now
        version = self._current_version()
        if version == self._version:
            return
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    knowledge_documents.c.source,
                    knowledge_documents.c.title,
                    knowledge_documents.c.url,
                    knowledge_chunks.c.seq,
                    knowledge_chunks.c.text,
                    knowledge_chunks.c.embedding,
                )
                .join(
                    knowledge_documents, knowledge_documents.c.doc_id == knowledge_chunks.c.doc_id
                )
                .where(knowledge_chunks.c.embedder == self.embedder.name)
                .order_by(knowledge_documents.c.source, knowledge_chunks.c.seq)
            ).all()
        if rows:
            matrix = np.asarray([r.embedding for r in rows], dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrix /= np.where(norms == 0, 1.0, norms)
        else:
            matrix = np.zeros((0, 1), np.float32)
        self._matrix = matrix
        self._meta = [(r.source, r.title, r.url, r.seq, r.text) for r in rows]
        self._version = version

    @property
    def default_min_score(self) -> float:
        """Calibrated on the seed corpus: with BGE-base, unrelated queries ("recipe for
        chocolate cake", a ModuleNotFoundError) score <= 0.53 and relevant ones >= 0.62.
        Hashing scores are lexical and lower across the board."""
        return 0.58 if "bge" in self.embedder.name.lower() else 0.15

    @property
    def size(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._meta)

    def search(self, query: str, k: int, min_score: float = 0.2) -> list[KnowledgeHit]:
        """Top-k chunks by cosine similarity above ``min_score``, at most two per paper so
        one long paper cannot crowd out the rest."""
        if not query.strip() or k <= 0:
            return []
        with self._lock:
            self._refresh()
            if not self._meta:
                return []
            (q,) = self.embedder.embed_query([query])
            vec = np.asarray(q, dtype=np.float32)
            norm = float(np.linalg.norm(vec))
            if norm == 0.0:
                return []
            scores = self._matrix @ (vec / norm)
            order = np.argsort(-scores)[: max(k * 3, k)]
            hits: list[KnowledgeHit] = []
            per_doc: dict[str, int] = {}
            for i in order:
                score = float(scores[i])
                if score < min_score:
                    break
                source, title, url, seq, text = self._meta[int(i)]
                if per_doc.get(source, 0) >= 2:  # keep the evidence diverse
                    continue
                per_doc[source] = per_doc.get(source, 0) + 1
                hits.append(
                    KnowledgeHit(f"ref:{_slug(source)}#{seq}", source, title, url, seq, text, score)
                )
                if len(hits) == k:
                    break
            return hits


def reference_records(
    index: KnowledgeIndex | None, query: str, k: int, min_score: float | None
) -> tuple[list[Record], dict[str, str]]:
    """Knowledge hits as model records (kind REFERENCE) plus ``record_id -> title`` for
    display. Retrieval is local: the query never leaves the server (ADR-S10)."""
    if index is None or k <= 0:
        return [], {}
    floor = index.default_min_score if min_score is None else min_score
    try:
        hits = index.search(query, k, min_score=floor)
    except Exception:  # the knowledge base is an enhancement; never fail the request
        return [], {}
    records = [
        Record(
            record_id=h.record_id,
            kind=RecordKind.REFERENCE,
            body=f"{h.title}\n\n{h.text}",
            source_url=h.url,
        )
        for h in hits
    ]
    return records, {h.record_id: h.title for h in hits}
