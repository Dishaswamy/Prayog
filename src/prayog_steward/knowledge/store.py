"""Ingesting papers into the knowledge base (run with the owner or maintenance role).

Idempotent per (document, embedder): re-ingesting an unchanged PDF does nothing. A changed
PDF replaces its chunks. Each chunk passes through the sanitiser (author e-mails and the
like), because chunks are later sent to the model (ADR-S03).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Engine, delete, func, select

from prayog_steward.db.models import knowledge_chunks, knowledge_documents
from prayog_steward.knowledge.text import chunk_words, clean_text, extract_pdf_text
from prayog_steward.rag.embed import Embedder
from prayog_steward.rag.sanitiser import Sanitiser


@dataclass(frozen=True)
class DocumentInput:
    source: str
    title: str
    authors: list[str]
    url: str | None
    published: date | None
    text: str  # cleaned body text


@dataclass(frozen=True)
class IngestResult:
    source: str
    title: str
    chunks: int
    skipped: bool


def document_from_pdf(pdf: Path) -> DocumentInput:
    """A PDF plus its optional ``<stem>.json`` sidecar from ``knowledge.arxiv.download``."""
    raw = extract_pdf_text(pdf)
    sidecar = pdf.with_suffix(".json")
    meta = json.loads(sidecar.read_text("utf-8")) if sidecar.exists() else {}
    if meta.get("arxiv_id"):
        source = f"arxiv:{meta['arxiv_id']}"
    else:
        source = f"file:{hashlib.sha256(pdf.read_bytes()).hexdigest()[:32]}"
    published = meta.get("published")
    return DocumentInput(
        source=source,
        title=meta.get("title") or pdf.stem.replace("_", " "),
        authors=list(meta.get("authors", [])),
        url=meta.get("abs_url"),
        published=date.fromisoformat(published) if published else None,
        text=clean_text(raw),
    )


def ingest(
    engine: Engine,
    doc: DocumentInput,
    embedder: Embedder,
    sanitiser: Sanitiser,
    now: datetime,
    *,
    chunk_size: int = 500,
    overlap: int = 150,
) -> IngestResult:
    digest = hashlib.sha256(doc.text.encode()).hexdigest()
    pieces = [sanitiser.sanitise(c).text for c in chunk_words(doc.text, chunk_size, overlap)]
    with engine.begin() as conn:
        existing = conn.execute(
            select(knowledge_documents.c.doc_id, knowledge_documents.c.content_sha256).where(
                knowledge_documents.c.source == doc.source
            )
        ).first()
        if existing is not None:
            have = conn.execute(
                select(func.count()).where(
                    knowledge_chunks.c.doc_id == existing.doc_id,
                    knowledge_chunks.c.embedder == embedder.name,
                )
            ).scalar_one()
            if existing.content_sha256 == digest and have == len(pieces):
                return IngestResult(doc.source, doc.title, have, skipped=True)
            doc_id = existing.doc_id
            conn.execute(
                knowledge_documents.update()
                .where(knowledge_documents.c.doc_id == doc_id)
                .values(
                    title=doc.title,
                    authors=doc.authors,
                    url=doc.url,
                    published=doc.published,
                    content_sha256=digest,
                    ingested_at=now,
                )
            )
            conn.execute(
                delete(knowledge_chunks).where(
                    knowledge_chunks.c.doc_id == doc_id,
                    knowledge_chunks.c.embedder == embedder.name,
                )
            )
        else:
            doc_id = uuid.uuid4()
            conn.execute(
                knowledge_documents.insert().values(
                    doc_id=doc_id,
                    source=doc.source,
                    title=doc.title,
                    authors=doc.authors,
                    url=doc.url,
                    published=doc.published,
                    content_sha256=digest,
                    ingested_at=now,
                )
            )
        if pieces:
            vectors = embedder.embed(pieces)
            conn.execute(
                knowledge_chunks.insert(),
                [
                    {
                        "chunk_id": uuid.uuid4(),
                        "doc_id": doc_id,
                        "seq": seq,
                        "text": text,
                        "embedder": embedder.name,
                        "embedding": vec,
                    }
                    for seq, (text, vec) in enumerate(zip(pieces, vectors, strict=True))
                ],
            )
    return IngestResult(doc.source, doc.title, len(pieces), skipped=False)


@dataclass(frozen=True)
class DocumentSummary:
    source: str
    title: str
    authors: list[str]
    url: str | None
    published: date | None
    chunks: int


def list_documents(engine: Engine, embedder_name: str | None = None) -> list[DocumentSummary]:
    count = func.count(knowledge_chunks.c.chunk_id)
    join_on = knowledge_chunks.c.doc_id == knowledge_documents.c.doc_id
    if embedder_name is not None:
        join_on = join_on & (knowledge_chunks.c.embedder == embedder_name)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                knowledge_documents.c.source,
                knowledge_documents.c.title,
                knowledge_documents.c.authors,
                knowledge_documents.c.url,
                knowledge_documents.c.published,
                count.label("chunks"),
            )
            .select_from(knowledge_documents.outerjoin(knowledge_chunks, join_on))
            .group_by(knowledge_documents.c.doc_id)
            .order_by(knowledge_documents.c.title)
        ).all()
    return [
        DocumentSummary(r.source, r.title, list(r.authors), r.url, r.published, int(r.chunks))
        for r in rows
    ]
