"""Knowledge base end to end (ADR-S10): real PDFs → ingest → index → S3/S4 citing papers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from test_api import ALICE, CSRF, bearer, open_session

from prayog_steward.db.models import knowledge_chunks, knowledge_documents
from prayog_steward.knowledge.index import KnowledgeIndex
from prayog_steward.knowledge.store import document_from_pdf, ingest, list_documents
from prayog_steward.rag.embed import HashingEmbedder
from prayog_steward.rag.sanitiser import Sanitiser
from steward_contracts import S3Input, S4Input
from steward_runtime.heuristic import HeuristicReasoning

fitz = pytest.importorskip("fitz")

WARMUP = (
    "Large minibatch training is unstable early on. We use a gradual warmup of the learning "
    "rate over the first five epochs, then apply the linear scaling rule: when the minibatch "
    "size is multiplied by k, multiply the learning rate by k. Contact: author@example.org. "
) * 30
MEMORY = (
    "Training deep networks is limited by GPU memory. Gradient checkpointing stores a subset "
    "of activations and recomputes the rest during the backward pass, trading compute for a "
    "sublinear memory cost and avoiding CUDA out of memory errors. "
) * 30


def make_pdf(path: Path, body: str, arxiv_id: str | None = None, title: str = "") -> Path:
    doc = fitz.open()
    words = body.split()
    for start in range(0, len(words), 350):  # several pages
        page = doc.new_page()
        page.insert_textbox(
            fitz.Rect(40, 40, 560, 800), " ".join(words[start : start + 350]), fontsize=9
        )
    doc.save(path)
    doc.close()
    if arxiv_id:
        meta = {
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": ["A. Author"],
            "published": "2017-06-08",
            "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        }
        path.with_suffix(".json").write_text(json.dumps(meta), "utf-8")
    return path


@pytest.fixture
def corpus(tmp_path: Path, admin_engine: Engine) -> Path:
    make_pdf(tmp_path / "1706.02677.pdf", WARMUP, "1706.02677", "Accurate, Large Minibatch SGD")
    make_pdf(
        tmp_path / "1604.06174.pdf",
        MEMORY,
        "1604.06174",
        "Training Deep Nets with Sublinear Memory Cost",
    )
    now = datetime.now(UTC)
    for pdf in sorted(tmp_path.glob("*.pdf")):
        ingest(admin_engine, document_from_pdf(pdf), HashingEmbedder(), Sanitiser(), now)
    return tmp_path


def _count(engine: Engine, table: Any) -> int:
    with engine.connect() as c:
        return int(c.execute(select(func.count()).select_from(table)).scalar_one())


def test_pdfs_are_extracted_chunked_sanitised_and_stored(
    corpus: Path, admin_engine: Engine
) -> None:
    docs = list_documents(admin_engine)
    assert {d.source for d in docs} == {"arxiv:1706.02677", "arxiv:1604.06174"}
    assert all(d.chunks >= 2 for d in docs)
    with admin_engine.connect() as c:
        bodies = " ".join(r[0] for r in c.execute(select(knowledge_chunks.c.text)))
    assert "author@example.org" not in bodies and "[REDACTED:EMAIL]" in bodies  # ADR-S03


def test_reingest_is_idempotent_and_changes_replace_chunks(
    corpus: Path, admin_engine: Engine
) -> None:
    before = _count(admin_engine, knowledge_chunks)
    pdf = corpus / "1706.02677.pdf"
    again = ingest(
        admin_engine, document_from_pdf(pdf), HashingEmbedder(), Sanitiser(), datetime.now(UTC)
    )
    assert again.skipped and _count(admin_engine, knowledge_chunks) == before
    make_pdf(pdf, WARMUP[: len(WARMUP) // 3], "1706.02677", "Accurate, Large Minibatch SGD")
    changed = ingest(
        admin_engine, document_from_pdf(pdf), HashingEmbedder(), Sanitiser(), datetime.now(UTC)
    )
    assert not changed.skipped
    assert _count(admin_engine, knowledge_documents) == 2
    assert _count(admin_engine, knowledge_chunks) < before


def test_application_role_can_read_but_never_change_the_corpus(
    corpus: Path, app_engine: Engine
) -> None:
    assert _count(app_engine, knowledge_documents) == 2
    for stmt in (
        "DELETE FROM steward.knowledge_chunks",
        "UPDATE steward.knowledge_documents SET title = 'x'",
    ):
        with app_engine.connect() as c, pytest.raises(DBAPIError):
            c.execute(text(stmt))


def test_index_ranks_the_relevant_paper_first(corpus: Path, app_engine: Engine) -> None:
    index = KnowledgeIndex(app_engine, HashingEmbedder(), refresh_seconds=0)
    hits = index.search(
        "CUDA out of memory: reduce activation memory with checkpointing", 3, min_score=0.0
    )
    assert hits and hits[0].source == "arxiv:1604.06174"
    assert hits[0].record_id.startswith("ref:arxiv-1604.06174#")
    assert hits[0].url == "https://arxiv.org/abs/1604.06174"
    assert len({h.source for h in hits}) >= 1 and sum(h.source == hits[0].source for h in hits) <= 2


def test_s3_answer_cites_papers_with_literature_provenance(
    corpus: Path, world: dict[str, Any]
) -> None:
    world["steward"].s3.reasoning = HeuristicReasoning()
    world["steward"].settings.knowledge_min_score = 0.0
    c = open_session(world)
    r = c.post(
        "/steward/s3/messages",
        json={"question": "learning rate warmup for large minibatch"},
        headers=CSRF,
    )
    answer = r.json()
    assert r.status_code == 200 and answer["outcome"] == "ok", answer
    literature = [cl for cl in answer["claims"] if cl["provenance"] == "literature"]
    assert literature, answer
    rid = literature[0]["cites"][0]
    assert answer["sources"][rid]["kind"] == "reference"
    assert answer["sources"][rid]["title"] in {
        "Accurate, Large Minibatch SGD",
        "Training Deep Nets with Sublinear Memory Cost",
    }
    assert any("published papers" in n for n in answer["notices"])


def test_s4_suggestions_cite_papers_and_list_them_as_sources(
    corpus: Path, world: dict[str, Any]
) -> None:
    world["steward"].s4.reasoning = HeuristicReasoning()
    world["steward"].settings.knowledge_min_score = 0.0
    r = world["client"].post("/steward/attempts/a2/optimize", headers=bearer(ALICE))
    answer = r.json()
    assert answer["outcome"] == "ok", answer
    titled = [s for s in answer["sources"].values() if s["title"]]
    assert titled and all(s["url"].startswith("https://arxiv.org/abs/") for s in titled)


def test_model_input_carries_reference_records(corpus: Path, world: dict[str, Any]) -> None:
    world["steward"].settings.knowledge_min_score = 0.0
    world["client"].post("/steward/attempts/a2/optimize", headers=bearer(ALICE))
    payload = world["stub"].calls[-1].payload
    assert isinstance(payload, S4Input)
    assert any(r.kind.value == "reference" for r in payload.records)
    c = open_session(world)
    c.post("/steward/s3/messages", json={"question": "warmup"}, headers=CSRF)
    s3 = world["stub"].calls[-1].payload
    assert isinstance(s3, S3Input) and any(r.kind.value == "reference" for r in s3.records)


def test_documents_endpoint_lists_the_corpus(corpus: Path, world: dict[str, Any]) -> None:
    docs = world["client"].get("/steward/knowledge/documents", headers=bearer(ALICE)).json()
    assert {d["source"] for d in docs} == {"arxiv:1706.02677", "arxiv:1604.06174"}
    assert world["client"].get("/steward/knowledge/documents").status_code == 401
