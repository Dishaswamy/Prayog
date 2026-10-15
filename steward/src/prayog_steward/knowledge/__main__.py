"""Knowledge-base command line.

    python -m prayog_steward.knowledge fetch --ids-file knowledge/seed_arxiv.txt
    python -m prayog_steward.knowledge fetch --topic "mixed precision training" --max 10
    python -m prayog_steward.knowledge ingest --pdf-dir .knowledge_cache
    python -m prayog_steward.knowledge list
    python -m prayog_steward.knowledge search "loss diverges after warmup"

``ingest`` writes to the corpus, so it uses ``--database-url``, else
STEWARD_MAINTENANCE_DATABASE_URL, else STEWARD_DATABASE_URL. The request-path application
role only has SELECT on the knowledge tables (migration 0002).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from prayog_steward.config import StewardSettings
from prayog_steward.db.engine import make_engine
from prayog_steward.knowledge import arxiv
from prayog_steward.knowledge.index import KnowledgeIndex
from prayog_steward.knowledge.store import document_from_pdf, ingest, list_documents
from prayog_steward.rag.embed import build_embedder
from prayog_steward.rag.sanitiser import Sanitiser

DEFAULT_CACHE = Path(".knowledge_cache")


def _ids(args: argparse.Namespace) -> list[str]:
    ids = list(args.ids or [])
    if args.ids_file:
        for line in Path(args.ids_file).read_text("utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                ids.append(line)
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prayog_steward.knowledge")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download arXiv PDFs and metadata")
    fetch.add_argument("--ids", nargs="*", help="arXiv identifiers, e.g. 1706.02677")
    fetch.add_argument("--ids-file", help="file with one arXiv identifier per line")
    fetch.add_argument("--topic", help="search arXiv for a topic instead")
    fetch.add_argument("--max", type=int, default=10)
    fetch.add_argument("--out", type=Path, default=DEFAULT_CACHE)

    ing = sub.add_parser("ingest", help="extract, chunk, embed and store PDFs")
    ing.add_argument("--pdf-dir", type=Path, default=DEFAULT_CACHE)
    ing.add_argument("--database-url")
    ing.add_argument("--embedder", help="hashing | st:<model> | auto")

    lst = sub.add_parser("list", help="list ingested documents")
    lst.add_argument("--database-url")

    srch = sub.add_parser("search", help="try a query against the corpus")
    srch.add_argument("query")
    srch.add_argument("-k", type=int, default=5)
    srch.add_argument("--database-url")
    srch.add_argument("--embedder")

    args = parser.parse_args(argv)
    settings = StewardSettings()

    if args.command == "fetch":
        if args.topic:
            papers = arxiv.search(args.topic, args.max)
        else:
            ids = _ids(args)
            if not ids:
                parser.error("give --ids, --ids-file or --topic")
            papers = arxiv.lookup(ids)
        for path in arxiv.download(papers, args.out):
            print(f"saved {path}")
        return 0

    url = (
        getattr(args, "database_url", None)
        or settings.maintenance_database_url
        or settings.database_url
    )
    engine = make_engine(url)

    if args.command == "list":
        for d in list_documents(engine):
            print(f"{d.chunks:4d} chunks  {d.source:24s}  {d.title}")
        return 0

    embedder = build_embedder(args.embedder or settings.knowledge_embedder)
    if args.command == "search":
        for hit in KnowledgeIndex(engine, embedder).search(args.query, args.k, min_score=0.0):
            print(f"{hit.score:.3f}  {hit.record_id}  {hit.title}\n       {hit.text[:200]}...")
        return 0

    pdfs = sorted(args.pdf_dir.glob("*.pdf"))
    if not pdfs:
        print(f"no PDFs in {args.pdf_dir}", file=sys.stderr)
        return 1
    sanitiser = Sanitiser()
    print(f"embedder: {embedder.name}")
    for pdf in pdfs:
        try:
            result = ingest(engine, document_from_pdf(pdf), embedder, sanitiser, datetime.now(UTC))
        except Exception as exc:  # one bad PDF must not stop the batch
            print(f"FAILED {pdf.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        state = "unchanged" if result.skipped else f"{result.chunks} chunks"
        print(f"{state:>12}  {result.source}  {result.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
