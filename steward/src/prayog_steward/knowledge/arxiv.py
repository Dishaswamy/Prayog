"""Fetching papers from arXiv (ported from Scientific-RAG-System/Download_papers.ipynb).

Uses the public export API directly over httpx instead of the ``arxiv`` package. It honours
arXiv's guidance: an identifying User-Agent and at least three seconds between requests.
Each PDF is saved with a JSON sidecar of its metadata, so ingestion is reproducible offline.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

API = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
USER_AGENT = "PrayogSteward/0.1 (CS455 course project; knowledge-base ingest)"
POLITE_DELAY_SECONDS = 3.0
_ID = re.compile(r"^\d{4}\.\d{4,5}$")


@dataclass(frozen=True)
class PaperMeta:
    arxiv_id: str
    title: str
    authors: list[str]
    published: str | None  # ISO date
    abs_url: str
    pdf_url: str


def parse_feed(xml_text: str) -> list[PaperMeta]:
    root = ET.fromstring(xml_text)
    papers: list[PaperMeta] = []
    for entry in root.findall("a:entry", ATOM):
        abs_url = (entry.findtext("a:id", default="", namespaces=ATOM) or "").strip()
        match = re.search(r"abs/([^v\s]+)", abs_url)
        if not match:
            continue
        arxiv_id = match.group(1)
        title = " ".join((entry.findtext("a:title", default="", namespaces=ATOM)).split())
        authors = [
            " ".join((a.findtext("a:name", default="", namespaces=ATOM)).split())
            for a in entry.findall("a:author", ATOM)
        ]
        published = (entry.findtext("a:published", default="", namespaces=ATOM) or "")[:10]
        papers.append(
            PaperMeta(
                arxiv_id=arxiv_id,
                title=title,
                authors=authors,
                published=published or None,
                abs_url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
            )
        )
    return papers


def _client() -> httpx.Client:
    return httpx.Client(timeout=60.0, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def lookup(ids: Iterable[str], client: httpx.Client | None = None) -> list[PaperMeta]:
    wanted = [i.strip() for i in ids if i.strip()]
    bad = [i for i in wanted if not _ID.match(i)]
    if bad:
        raise ValueError(f"not arXiv identifiers: {bad}")
    http = client or _client()
    response = http.get(API, params={"id_list": ",".join(wanted), "max_results": len(wanted)})
    response.raise_for_status()
    return parse_feed(response.text)


def search(topic: str, max_results: int, client: httpx.Client | None = None) -> list[PaperMeta]:
    http = client or _client()
    response = http.get(
        API,
        params={
            "search_query": f'all:"{topic}"',
            "max_results": max_results,
            "sortBy": "relevance",
        },
    )
    response.raise_for_status()
    return parse_feed(response.text)


def download(
    papers: Iterable[PaperMeta],
    out_dir: Path,
    client: httpx.Client | None = None,
    delay: float = POLITE_DELAY_SECONDS,
) -> list[Path]:
    """Download PDFs (skipping ones already present) and write ``<id>.json`` sidecars."""
    out_dir.mkdir(parents=True, exist_ok=True)
    http = client or _client()
    saved: list[Path] = []
    for i, paper in enumerate(papers):
        pdf = out_dir / f"{paper.arxiv_id}.pdf"
        (out_dir / f"{paper.arxiv_id}.json").write_text(
            json.dumps(asdict(paper), indent=1), "utf-8"
        )
        if not pdf.exists():
            if i:
                time.sleep(delay)
            response = http.get(paper.pdf_url)
            response.raise_for_status()
            if not response.content.startswith(b"%PDF"):
                raise ValueError(f"{paper.arxiv_id}: response is not a PDF")
            pdf.write_bytes(response.content)
        saved.append(pdf)
    return saved
