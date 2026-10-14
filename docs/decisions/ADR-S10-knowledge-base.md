# ADR-S10 — A shared knowledge base of reference papers for S3 and S4

**Status:** Accepted (2026-10-14; decided with the subsystem owner) · **Source:** the team's `Scientific-RAG-System` notebooks · **Requirements:** FR-STW-006, 012, 016, 017, NFR-SEC-002, ADR-S03, ADR-S09

## Context
A teammate built a scientific-paper RAG prototype in two notebooks. It downloads arXiv papers, extracts and cleans PDF text, makes 500-word chunks, embeds them with `BAAI/bge-base-en-v1.5`, stores them in a local Qdrant collection, retrieves the top 5 and answers with Gemini through LangChain. The Steward needs grounded reference material: S4's optimisation advice and S3's explanations are better when they rest on published findings, not only on the model's memory.

## Decision
- **Role:** the paper corpus is a *shared knowledge base* that S3 and S4 retrieve from. It is not a fifth context (ID-05 is unchanged).
- **Ported from the notebooks:** PDF extraction (PyMuPDF), text cleaning, word-window chunking (500 words, overlap 150), BGE embeddings and arXiv fetching (`prayog_steward.knowledge`).
- **Fixed while porting:**
  - References are cut only at a heading in the latter part of the paper; the notebook cut at the first occurrence of the word.
  - De-hyphenation only joins words split across a line break.
  - arXiv side-stamps are removed.
- **Not adopted:**
  - **Gemini and LangChain.** Generation stays on the Steward's Claude adapter, behind the same sanitiser, citation validation and audit as everything else: one provider, one key.
  - **Qdrant.** The corpus lives in PostgreSQL (`steward.knowledge_documents`, `steward.knowledge_chunks`, migration 0002). Search is one numpy matrix-vector product over the active embedder's chunks, which takes milliseconds at the planned size. This keeps one database, as in ADR-S09.
- **Security:**
  - The corpus is public literature, so it is not under RLS.
  - The application role has **SELECT only**, and only the ingest command (owner or maintenance role) writes. A compromised request path cannot poison what the Steward cites.
  - Chunks pass through the sanitiser before storage (author e-mails and similar), because they are sent to the model.
  - Retrieval is local, so queries never leave the server.
- **Provenance:** a new record kind `reference` and claim provenance `literature`. The server enforces that literature claims cite only reference records and that internal claims never rest on papers (FR-STW-017). The UI labels them "Literature" with the paper's title and arXiv link.
- **Embeddings:** `STEWARD_KNOWLEDGE_EMBEDDER=auto` uses BGE-base (the notebook's model) when the `[knowledge]` extra is installed, and the hashing embedder otherwise. Ingest and query must use the same embedder; chunks are stored per embedder.
- **Seed corpus:** 15 papers on what the Steward actually diagnoses, listed with reasons in `knowledge/seed_arxiv.txt`: memory limits, warmup and large batches, mixed precision, optimisers, schedules, scaling. PDFs are cached in `.knowledge_cache/` and are never committed.

## Consequences
- S4 suggestions can cite, for example, Goyal et al. on warmup with a linear learning-rate rule, next to the user's own metrics.
- BGE pulls in torch (~200 MB). Without it the system still works with lexical retrieval.
- **Security note on the source notebooks:** `RAG.ipynb` contains a hard-coded Google API key. It must be revoked and removed from that repository's history. Nothing from that cell was carried over.
