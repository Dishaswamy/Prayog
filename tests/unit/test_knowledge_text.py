"""Knowledge base pieces that need no database (ADR-S10)."""

from __future__ import annotations

from typing import Any

import pytest

from factories import ok_response, s3_request
from prayog_steward.coordinator.validation import validate_response
from prayog_steward.knowledge.arxiv import parse_feed
from prayog_steward.knowledge.index import reference_records
from prayog_steward.knowledge.text import chunk_words, clean_text
from steward_contracts import (
    InvokeRequest,
    Provenance,
    Record,
    RecordKind,
    S3Input,
    S3Turn,
    S4Input,
)
from steward_runtime.heuristic import HeuristicReasoning

# ---------------------------------------------------------------- cleaning (ported + fixed)

BODY = "\n".join(f"Sentence {i} about warmup and learning rates in training." for i in range(40))


def test_references_are_cut_only_at_a_late_heading() -> None:
    early = "Related work on References to prior art\n"  # not a heading
    paper = early + BODY + "\nReferences\n[1] Someone. A paper. 2020."
    out = clean_text(paper)
    assert "Sentence 39" in out  # the notebook's split would have dropped all of this
    assert "[1] Someone" not in out


def test_hyphenation_is_repaired_only_across_line_breaks() -> None:
    out = clean_text("an exam-\nple of state- of-the-art work")
    assert "example" in out and "state- of-the-art" in out


def test_captions_tables_page_numbers_and_arxiv_stamps_are_removed() -> None:
    raw = "\n".join(
        [
            "arXiv:1706.02677v2 [cs.CV] 30 Apr 2018",
            "Figure 3: training curves",
            "ResNet-50 76.3 75.9 75.2 74.8",
            "12",
            "The body text stays.",
        ]
    )
    assert clean_text(raw) == "The body text stays."


def test_control_characters_from_pdf_extraction_are_removed() -> None:
    """Real arXiv PDFs yield NUL bytes, which PostgreSQL text columns reject."""
    out = clean_text("Adam is\x00 an optimiser\x07 for\x1f training.")
    assert out == "Adam is an optimiser for training."


def test_word_chunks_overlap_and_drop_tiny_tails() -> None:
    text = " ".join(f"w{i}" for i in range(1200))
    chunks = chunk_words(text, chunk_size=500, overlap=150, min_words=50)
    assert [len(c.split()) for c in chunks] == [500, 500, 500]
    assert chunks[1].split()[0] == "w350"  # 500 - 150
    assert chunk_words("too short", min_words=50) == []
    with pytest.raises(ValueError):
        chunk_words(text, chunk_size=100, overlap=100)


# ---------------------------------------------------------------- arXiv feed

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.02677v2</id>
    <published>2017-06-08T17:53:42Z</published>
    <title>Accurate, Large Minibatch SGD:
      Training ImageNet in 1 Hour</title>
    <author><name>Priya Goyal</name></author>
    <author><name>Kaiming He</name></author>
  </entry>
</feed>"""


def test_arxiv_feed_is_parsed_into_metadata() -> None:
    (paper,) = parse_feed(FEED)
    assert paper.arxiv_id == "1706.02677"
    assert paper.title == "Accurate, Large Minibatch SGD: Training ImageNet in 1 Hour"
    assert paper.authors == ["Priya Goyal", "Kaiming He"]
    assert paper.published == "2017-06-08"
    assert paper.pdf_url == "https://arxiv.org/pdf/1706.02677"


# ---------------------------------------------------------------- provenance (FR-STW-017)


def _with_reference() -> InvokeRequest:
    request = s3_request()
    payload = request.payload
    assert isinstance(payload, S3Input)
    ref = Record(
        record_id="ref:arxiv-1706.02677#3",
        kind=RecordKind.REFERENCE,
        body="Accurate, Large Minibatch SGD\n\nUse a gradual warmup.",
        source_url="https://arxiv.org/abs/1706.02677",
    )
    return request.model_copy(
        update={"payload": payload.model_copy(update={"records": [*payload.records, ref]})}
    )


def _claim(text: str, provenance: str, cites: list[str]) -> dict[str, Any]:
    return {"text": text, "provenance": provenance, "cites": cites}


def test_literature_claims_must_rest_on_reference_records_only() -> None:
    request = _with_reference()
    result = {
        "claims": [
            _claim("Warmup helps large batches.", "literature", ["ref:arxiv-1706.02677#3"]),
            _claim("Paper posing as the user's record.", "internal", ["ref:arxiv-1706.02677#3"]),
            _claim("User record posing as literature.", "literature", ["led:1"]),
            _claim("Web posing as literature.", "literature", ["web:1"]),
        ],
        "steps": [],
        "evidence_found": False,
    }
    v = validate_response(request, ok_response(request, result))
    assert isinstance(v.result, S3Turn)
    assert [(c.text, c.provenance) for c in v.result.claims] == [
        ("Warmup helps large batches.", Provenance.LITERATURE)
    ]


def test_heuristic_uses_literature_and_passes_validation() -> None:
    request = _with_reference()
    v = validate_response(request, HeuristicReasoning().invoke(request))
    assert isinstance(v.result, S3Turn) and v.stripped == 0
    assert any(c.provenance is Provenance.LITERATURE for c in v.result.claims)

    s4 = InvokeRequest(
        invocation_id="i4",
        payload=S4Input(
            attempt_id="a",
            records=[
                Record(record_id="met:1", kind=RecordKind.METRICS, body='{"val_loss": 0.4}'),
                Record(
                    record_id="ref:arxiv-1608.03983#0",
                    kind=RecordKind.REFERENCE,
                    body="SGDR: Stochastic Gradient Descent with Warm Restarts\n\ntext",
                ),
            ],
        ),
    )
    v4 = validate_response(s4, HeuristicReasoning().invoke(s4))
    assert v4.result is not None and v4.stripped == 0


def test_no_knowledge_base_means_no_references() -> None:
    assert reference_records(None, "anything", 3, 0.3) == ([], {})
