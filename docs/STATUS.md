# Status: Steward subsystem (2026-10-08)

## Built

All four contexts work end to end against a real PostgreSQL, behind the HTTP API, in the web UI and from the CLI:

- **S1:** seal hook in the platform's transaction → outbox task → scoped, sanitised input → model → server-side validation (contract plus citation stripping) → append-only diagnosis, enum-only hardware finding, bounded proposal → owner approval through ordinary submission (idempotent, refused after expiry).
- **S2:** deterministic threshold (distinct users, window, cooldown, per-unit advisory lock) → OPEN warning → advisory S2 correlation over a metadata-only bundle → admin close with reason consumes evidence. Admin view is metadata only.
- **S3:** single-use 15-minute links stored hashed, with audited denial reasons → HttpOnly session → RAG ingestion and retrieval under row-level security per user and attempt → allow-listed web search → provenance-labelled answer, manual steps only, per-user/attempt cache.
- **S4:** owner's REBUILD_SUCCESS only; own permitted ledger data; advisory output.
- **Cross-cutting:** sanitiser in front of every egress, retention purge, audit events, security headers, CSRF, no stack traces, retries with leases, outage handling.

## Update 2026-10-14: knowledge base integrated (ADR-S10)

The team's `Scientific-RAG-System` notebooks are now part of the Steward as a shared knowledge base of reference papers.
- **What changed:**
  - The notebook code is ported into `prayog_steward.knowledge`: arXiv fetch, PDF extraction and cleaning, chunking, BGE embeddings.
  - The corpus is stored in PostgreSQL (migration 0002, read-only to the app role) and searched in memory.
  - S3 and S4 cite papers with a server-enforced "literature" provenance. Generation stays on our Claude adapter; Gemini/LangChain and Qdrant were not adopted.
- **Defects fixed while porting:**
  - The references cut could drop most of a paper.
  - De-hyphenation merged unrelated words.
  - NUL bytes from real PDFs crashed inserts.
- **Corpus:** the seed is 15 papers chosen for what the Steward diagnoses (299 chunks). Relevance was calibrated on it: BGE scores ≥ 0.62 for relevant queries and ≤ 0.53 for unrelated ones, so the floor is 0.58. A dependency-error question cites no papers; a warmup question cites Goyal et al. and LAMB.
- **Tests:** 216 pass, 113 traced to requirements, including real PDFs through ingest → index → S3/S4 over HTTP on PostgreSQL.
- **Action needed outside this repo:** `RAG.ipynb` in the teammate's repository contains a hard-coded Google API key. It must be revoked and scrubbed from that repo's history.

## Verified

| Check | Result |
|---|---|
| Test suite (unit + integration on real PostgreSQL, app role without BYPASSRLS) | 198 passed |
| ruff, mypy `--strict`, import-linter (Runtime has no server/DB imports) | clean |
| Traceability: 98 tests named against requirement IDs | 0 missing |
| Fresh clone → clean venv → full suite | passes |
| Wheel contains UI assets and migrations; migrations run from any directory | yes |
| Demo in a real browser: failed run → diagnosis → approve; failed rebuild → link → UI chat; replayed link refused; another user's cookie refused | yes |
| Three processes (Runtime over HTTP): Runtime stopped → run still seals, task waits, health shows unreachable; Runtime restarted → diagnosis appears | yes |
| Real Anthropic SDK request and response handling (mocked HTTP): schema, effort, cached system prompt, fallback beta, refusal, errors | yes |

## Not verified here (needs your environment)

1. **A live Claude call.** No API key on this machine. Run `ANTHROPIC_API_KEY=... python -m pytest -m live_llm tests/live` (a few cents).
2. **Docker.** Not installed here. `Dockerfile` and `docker-compose.yml` follow the processes verified above, but have not been built.
3. **GitHub Actions.** The workflow mirrors the local gate but has not run on GitHub yet.
4. **The real accuracy evaluation.** `eval/s1_cases.jsonl` is a synthetic seed set. The Project Document promises ~100 real, hand-labelled failed runs; add them with `"source": "real"` and run `python tools/evaluate.py --claude`.

## Decisions taken during the build (all recorded)

- **ADR-S09:** no pgvector. Embeddings are `real[]`, ranked in Python over the RLS-filtered candidate set. Any PostgreSQL 15+ works.
- **`HeuristicReasoning`:** a rule-based adapter for demos without a key, and the evaluation baseline.
- **Links outside RLS:** `steward_links` holds only hashes and IDs; this lets the consume path audit *why* a link was refused (expired, replayed, identity mismatch). All diagnostic context stays under RLS.
- **`messages.create` + `anthropic.transform_schema`** instead of `messages.parse`, because `parse` raised before a refusal could be recognised (found by the offline SDK test).
- **S3 web UI:** a dependency-free static page served by the API instead of a React build. All model text is rendered with `textContent`. The platform's React SPA can host or replace it later.
- **The S3 link binds to the CLI session** that requested it and dies on logout. The browser user must be the attempt owner (the SADD's "same session" check cannot work across CLI and browser).

## To merge into the team repo

1. Add the remote and push this history on a branch (`git remote add origin <url>`; `git push -u origin main:steward`). Then open a PR.
2. The other sub-teams implement the nine ports and make the four calls in [INTEGRATION.md](INTEGRATION.md).
3. Raise with the team: CR-S1 (failure signature, ADR-S05), the open defaults in ADR-S08, the platform cookie name and the `X-Steward-CSRF` convention (CR-H).
