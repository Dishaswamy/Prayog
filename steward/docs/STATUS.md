# Status: Steward subsystem

## Update 2026-10-15: merged into the Prayog repository and integrated with the backend

The Steward now lives in `steward/` of the team repository; its history is kept by a subtree merge. It is wired into the real backend by `backend/integration/steward_bridge.py`. What that means and where each piece lives: [INTEGRATION.md §0](INTEGRATION.md#0-status-in-this-repository-2026-10-15-integrated). How the result conforms to the SADD, and the four deviations the merge required: [SADD_CONFORMANCE.md](SADD_CONFORMANCE.md).

**Tests.** `backend/tests` holds 14 end-to-end tests on real PostgreSQL. They use real registration, access approval by professor and admin, `/jobs` submissions placed by real scheduling passes, and runs ended through `lifecycle.release()`. Disabling the seal-hook registration makes 7 of them fail, so they exercise the integration rather than pass vacuously. The Steward's own suite passes in the merged environment: 218 tests, plus lint, strict types, import rules and traceability.

**Observed live** (four separate processes: API :8000, Steward Runtime :8100, Steward Worker, scheduler tick, on database `prayog_live`):
1. **S1:** alice submitted a 2-GPU job, the triggered pass placed it on n1, and the simulated agent failed it with an OOM log. The Worker, through the Runtime over HTTP, produced a diagnosis citing the log; the planted API key was absent from it.
2. **Rebuilds:**
   - A snapshot alice never ran was `REBUILD_REJECTED` (404, no attempt).
   - A rebuild that failed read `REBUILD_FAILED`, and its Steward link opened in a real browser. The page showed the sign-in form, alice signed in through the platform's `/auth/login`, the link was consumed by POST, and the token disappeared from the URL.
   - The chat answered, citing the attempt's log and alice's job history.
   - S4 worked on a successful attempt; on the failed one it was refused (403).
3. **S2:** with n2 down, alice and bob failed on n1/gpu0. A warning was raised from two labs, the advisory S2 finding was attached, the admin saw it, and bob got 404.
4. **Outage:**
   - With the Runtime killed, a failed run still sealed (task PENDING), new jobs still scheduled, and admin health showed `runtime_reachable: false`.
   - After restart the Worker's retry finished the diagnosis within 10 seconds.
5. **Knowledge base:** after ingesting the 15-paper seed into `prayog_live`, S4 on a successful rebuild cited Smith (hyper-parameters) and LAMB with arXiv links. The first query after a restart used to take over 10 s while BGE loaded; `install_steward` now warms the model in a background thread, and the first query takes 0.17 s.

**Defects found by live evaluation and fixed** (each with a test):
- Steward timestamps came back as `+05:30` on a Postgres whose TimeZone is IST. Steward sessions now run in UTC, as the platform's API.md requires.
- The heuristic adapter listed every earlier failed run as "failed the same way". It now lists only earlier failures of the same class. Live check: a library-version failure matched only the earlier library-version run.
- With `STEWARD_REASONING` unset, the Runtime silently used the stub, which stored "Stub diagnosis." placeholders. It now defaults to the heuristic and refuses unknown values at startup.

**Also checked:**
- The platform migrations have a single head, and `b7e4c1a9d2f0` downgrades and re-upgrades cleanly.
- The audit log refuses UPDATE and DELETE even for a superuser.
- The Steward's database role has no privileges on any platform table.
- The API reference is [API.md](API.md).

**Still not verified:** a live Claude call (no API key on the dev machine), the Docker images, a CI run on GitHub, and the real ~100-case evaluation. Without a key, live proposals cannot appear: the heuristic's only proposal (batch size) is not a platform field ([SADD_CONFORMANCE.md](SADD_CONFORMANCE.md), deviation 3).

## Built (2026-10-08)

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
