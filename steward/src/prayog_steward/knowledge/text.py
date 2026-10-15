"""PDF text extraction, cleaning and chunking (ported from Scientific-RAG-System/RAG.ipynb).

Changes from the notebook, each fixing a defect found while porting:
- References are cut at a *heading* in the latter part of the paper. The notebook split on
  the first occurrence of the word "References" anywhere, which can drop most of a paper.
- Hyphenation is repaired only across line breaks (``exam-\\nple``). The notebook's
  ``-\\s+`` also merged unrelated words such as ``state- of``.
- arXiv side-stamps and running headers that are just page numbers are removed.
"""

from __future__ import annotations

import re
from pathlib import Path

_HEADING = re.compile(r"^\s*(?:\d+\.?\s*)?(references|bibliography)\s*$", re.I)
_ARXIV_STAMP = re.compile(r"^arXiv:\d{4}\.\d{4,5}(v\d+)?\s*\[[\w.\-]+\]", re.I)
_CAPTION = re.compile(r"^(figure|fig\.|table)\s*\d", re.I)
# NUL and other C0 controls (PDF extraction emits them; PostgreSQL text rejects NUL).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def extract_pdf_text(path: Path) -> str:
    """All page text of a PDF, in reading order as PyMuPDF yields it."""
    import fitz  # PyMuPDF; part of the [knowledge] extra

    with fitz.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def _is_table_line(line: str) -> bool:
    words = line.split()
    if len(words) < 4:
        return False
    numeric = sum(any(ch.isdigit() for ch in w) for w in words)
    return numeric / len(words) > 0.4


def clean_text(raw: str) -> str:
    """Body text of a paper as one normalised string: no captions, tables, page numbers,
    arXiv stamps or reference list."""
    text = _CONTROL.sub(" ", raw)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    lines = [line.strip() for line in text.split("\n")]
    kept: list[str] = []
    for i, line in enumerate(lines):
        if not line:
            continue
        if _HEADING.match(line) and i > 0.3 * len(lines):
            break  # the reference list starts here
        if _CAPTION.match(line) or _ARXIV_STAMP.match(line) or re.fullmatch(r"\d+", line):
            continue
        if _is_table_line(line):
            continue
        kept.append(line)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def chunk_words(
    text: str, chunk_size: int = 500, overlap: int = 150, min_words: int = 50
) -> list[str]:
    """Overlapping word windows, as in the notebook (500 words, 150 overlap)."""
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")
    words = text.split()
    chunks: list[str] = []
    start = 0
    while start < len(words):
        window = words[start : start + chunk_size]
        if len(window) >= min_words:
            chunks.append(" ".join(window))
        if start + chunk_size >= len(words):
            break
        start += chunk_size - overlap
    return chunks
