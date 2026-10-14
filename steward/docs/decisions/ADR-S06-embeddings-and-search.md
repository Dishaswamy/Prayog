# ADR-S06 — Local embeddings; allow-listed web search adapter

**Status:** Accepted (2026-10-07) · **Requirements:** FR-STW-012, 017, NFR-SEC-006, NFR-PERF-001

## Decision
- **Embeddings:** Anthropic offers no embeddings API, so we use local `sentence-transformers` (`BAAI/bge-small-en-v1.5`, 384 dimensions) on CPU behind an `Embedder` port. No data leaves the server, and nothing touches the scheduled GPU pool (NFR-PERF-001). This is the optional extra `[rag]`. Tests use a deterministic hashing embedder.
- **Web search:** a `SearchProvider` port with `none` (default), `tavily` and `brave` adapters. The query is built by `search.query_builder` from `{exception_class, packages[(name, version)]}` and nothing else. **Claude's server-side `web_search` tool is not used**, because the model would author the query text.
- Results are tagged `provenance="external"` with their URL, and are never presented as guaranteed fact (FR-STW-017). Search failure degrades to local context and says so (A20).
