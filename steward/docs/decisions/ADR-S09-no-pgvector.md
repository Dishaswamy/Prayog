# ADR-S09 — Store embeddings as `real[]`; rank in the application; no pgvector

**Status:** Accepted (2026-10-08). Supersedes the "pgvector" part of SADD CD-02 and ADR-19. Row-level security from ADR-19 is unchanged.

## Context
Every S3/S4 retrieval is restricted by row-level security to one user, and by the query to one rebuild attempt (or attempts the user names explicitly, A13). At Prayog's scale (60–90 accounts) that candidate set is tens to a few hundred chunks. An approximate-nearest-neighbour index buys nothing at that size. Meanwhile the `vector` extension is an extra deployment dependency: it isn't in stock PostgreSQL images and has no official Windows build, which blocked local testing.

## Decision
- `steward_private.rag_chunks.embedding` is `real[]`, with the embedder's name stored beside it.
- The retriever loads the RLS-filtered candidates for the attempt (bounded by `rag_max_candidates`) and ranks them by cosine similarity in Python.
- Any PostgreSQL 15+ works. CI and docker-compose use the stock `postgres:15` image.

## Consequences
- No extension to install, and the full database test suite runs on any machine with PostgreSQL.
- If candidate sets ever grow to tens of thousands, adding pgvector is a local change: a migration to convert the column plus an index, and the retriever's ranking moves into SQL. The isolation design does not change.
