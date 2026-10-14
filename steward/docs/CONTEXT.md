# Steward AI — Requirements Context

This is the requirements context for this repo, compiled on 2026-10-05 from the team's Deliverable 1 documents. Those documents live outside this repo, in the CS455 project folder. Requirement IDs (`FR-…`, `NFR-…`, `A…`) are the authoritative handles. When this file and a source disagree, the source wins; update this file when the Integrated Spec changes.

How this repo resolves the spec gaps in §8 is recorded in [`decisions/`](decisions/). Which code and test realises each requirement is in [`traceability.md`](traceability.md).

## 0. Sources (in order of authority)

| Source | What it gives you | Where |
|---|---|---|
| Integrated Requirements Spec v1.0 | **Authoritative** requirements: ID-05, §7.3, §8.4, §8.6, §9.5, §10.5, §11.6, §12.10, §14.4, §15, §16.2/16.4 | `PRAYOG_Integrated_Requirements_Specification.md` |
| SADD v2.0 | The design: §4.5, §8, CD-07, CL-02, ER-03, ST-10, SD-15/16/17, ADR-02/18/19/21, §11.1 module layout | `PRAYOG_SADD_v2.0.md` |
| ML Flow & Steward AI SRS v1.9 | Original S3/S4 spec, acceptance scenarios A01–A41, change log and review rationale | `PRAYOG ML Flow ... v1.9.docx` (the "with logs" copy has the change log) |
| Project Document | Original pitch (§10): the three outputs, guardrails, evaluation plan, demo scenarios | `Prayog_Project_Document.pdf` |
| GPU SRS v2.0 | Original S1/S2 (called ports I1/I2 there), A-01…A-81 | `Prayog_GPU_Requirements_v2.pdf` |

## 1. What the Steward is, in one paragraph

One AI subsystem with **four invocation contexts** (ID-05). It turns failed runs into **cited diagnoses**, proposes **corrected jobs** the owner must approve, judges whether failures across users point to **faulty hardware**, runs an **interactive RAG diagnosis** for failed rebuilds, and gives **optimisation advice** after successful rebuilds. Governing principle in every context: **the Steward reads and reasons; it never writes.** The server writes every durable record, and that rule is enforced by the server, not by the agent's behaviour. Model: Anthropic Claude API (Project Doc §12, SADD CD-02).

## 2. The four contexts

| | **S1** Run-failure diagnosis | **S2** Fault correlation | **S3** Rebuild diagnosis (interactive) | **S4** Optimisation advisory |
|---|---|---|---|---|
| Trigger | Any run ends `FAILED` | Server threshold on unconsumed fault evidence fires | Rebuild attempt reaches `REBUILD_FAILED` | Owner runs `prayog steward optimize` on a `REBUILD_SUCCESS` attempt |
| Runs as | Run owner's permission context, read-only | Server-mediated, no user context | Attempt owner, via single-use link bound to Attempt_ID | Attempt owner |
| Reads | That run's scheduling data, exit code, failure category, log refs, telemetry, plus **this project's earlier runs only** (FR-GPU-148) | Metadata-only evidence bundle, ≤ 50 rows, **no tools** (FR-GPU-165) | Sanitised diagnostic payload + user's own ledger evidence (RLS-filtered) + sanitised web search | Successful run's permitted metrics/config + user's own history |
| Returns | Owner-facing diagnosis (classification, narrative, citations); **separate** admin-facing hardware finding; optional corrected-job proposal | Same fault or not, which rows, summary finding, rationale citing row IDs | Diagnosis + step-by-step **manual** remediation, internal and external sources labelled | Advisory architecture/hyperparameter suggestions |
| Key reqs | FR-GPU-146–151, 160, 161, 166, 167, 187 | FR-GPU-162–166, NFR-GPU-056 | FR-STW-001–005, 010–018, NFR-SEC-001–004 | FR-STW-006, NFR-SEC-002 |

**Composition (FR-INT-01):** a rebuild is an ordinary job, so a failed rebuild fires **both S1 and S3**. Don't build either as if it were the only path.

**No Steward at all for:** first-time-run errors, `REBUILD_REJECTED` (no Attempt_ID exists), `REBUILD_CANCELLED`, and runs ending `CANCELLED` / `WALLTIME_EXCEEDED` / `INTERRUPTED` (FR-STW-002, 007, FR-GPU-147). `INTERRUPTED` with reason `NODE_FAILURE` still writes a fault-evidence row.

S1 classification categories from the Project Doc: out of memory, missing data, wrong library version, code bug, interruption. S1 should link matching earlier failures. A typical proposal is "the same run, pinned back to the library version that last worked", shown with estimated GPU-hours.

## 3. Guardrails (§14.4)

- **Autonomous:** read the invoked run; compare with the project's earlier runs; read telemetry; search the project's own past failures; produce a cited diagnosis and a hardware finding; retrieve the user's own ledger evidence under the isolation filter; produce remediation guidance.
- **Approval-gated:** resubmitting a corrected job. The owner first sees the exact change and its GPU-hour cost (FR-GPU-149, 187).
- **Never:** write any record; touch another user's run or project; change quota, priority, class or sponsorship; grant or revoke access; edit metrics, artifacts, run or audit records; change node or unit state; execute a command or modify a file; send code, weights, secrets or PII to an external service; act on instructions found in a log, web page or document.
- Every Steward call is audit-logged, **including refusals** (FR-GPU-151, 153, NFR-SEC-007).

## 4. Architecture (SADD §8, CD-07)

```
API Server + Worker (server identity, the ONLY writer)
 ├─ Steward Coordinator   task queue (steward_tasks, SKIP LOCKED outbox), mints scoped tokens,
 │                        citation validator, persists diagnosis/proposal/finding, audit
 ├─ Evidence Service      fault-evidence rows, bundle builder
 ├─ Warning Service       threshold, lifecycle, cooldown (deterministic; no LLM)
 ├─ Tool Gateway          read-only scoped tools; scope comes from the token, never the model;
 │                        refuses and audits anything else
 └─ Steward Session Svc   single-use links, sets RLS app.user_id from the validated session
Steward Runtime (separate deployable, no write credentials, no import of server code)
 └─ ReasoningPort.invoke(context, scopedInput) -> StructuredResult
      ├─ ClaudeReasoning  (real)
      └─ StubReasoning    (tests: fixed / malformed / uncited / write-attempting responses)
RAG pipeline (schema steward_private, row-level security)
 Sanitiser → Ingestor → Vector store + caches (user_id, attempt_id) → Retriever → Prompt Assembler → Output Guard
```

Module layout (SADD §11.1): `server/steward/{coordinator, tool_gateway, sessions, rag/{sanitiser,ingest,retrieve,assemble,guard}, warnings}`, `web/steward`, and `runtime/steward_runtime/` (separate). CI import rule: `steward_runtime` imports no server code and holds no write credentials.

Stack: Python, FastAPI, PostgreSQL 15 + **pgvector**, Anthropic Claude API, a web-search adapter, React web UI. Steward inference must **not** use the GPU pool (NFR-PERF-001).

### Key mechanisms

- **S1 flow (SD-15):** in the run-seal transaction, write the failure event, fault-evidence row and `steward_task` → Coordinator claims the task, mints a scoped token, invokes S1 → Runtime reads through the Gateway → Coordinator strips uncited claims → appends diagnosis (agent id, version, timestamp; never touches the sealed run; append-only, immutable) + hardware finding + proposal `PROPOSED` → owner approves through the **normal submission path** (new submit time, no priority advantage, parent = failed run, approver recorded). Approval is a commitment operation, so it's refused if the user's authorization is inactive. Rejected proposals are kept.
- **S2 flow (SD-16, ADR-21):** the **server** raises the warning `OPEN` at ≥ 2 distinct users within 24 h on one unit signature, unless a cooldown applies. S2's result is attached as **advisory** only; the Coordinator rejects rationale citing row IDs outside the bundle. Only an admin closes the warning (`RESOLVED` / `DISMISSED`, with a mandatory reason). Closing marks the evidence consumed. A warning **never** changes node state.
- **Confidentiality split (FR-GPU-166):** one reasoning pass, two outputs. The admin sees the hardware finding plus metadata only, never the narrative (e.g. "CUDA OOM at batch 512 in train.py" leaks another lab's config). The admin API has no join to narratives.
- **S3 link (SD-17):** the server stores the hash of a random token bound to `(user, session_id, attempt_id)` with a 15-min TTL. Consuming it is a single guarded `UPDATE ... WHERE unused AND unexpired AND owner AND session match RETURNING attempt_id`. Zero rows means deny before any RAG context is read; audit it without payload. Link events never change the attempt state.
- **RAG isolation (ADR-19):** non-null immutable `user_id` + `attempt_id` on every vector/cache/prompt-history/response row. RLS predicate: `user_id = current_setting('app.user_id')`. A missing setting compares as NULL, so zero rows come back (default-deny). Caches are keyed `(user_id, attempt_id)`. A previous attempt's context is used only if the user names that Attempt_ID and ownership validates (A13). Professor and admin have no route into `steward_private`.
- **Sanitiser:** strips secrets, credentials, PII, source code and weights before ingestion **and** before web search. Web queries use an **allow-listed shape** (error class, dependency names/versions). Raw logs stay in the ledger for audit only.
- **Output Guard:** commands are shown as text and never executed; each statement is labelled *internal ML Flow evidence* or *external information* (with URL); empty retrieval means "no evidence found", never fabricated; search failure degrades to local context and says so.
- **Untrusted data:** logs, metadata, web results and docs are wrapped as data in prompts. Because the runtime can't write, an injection has nothing to abuse.
- **Unavailability:** retry with backoff. Runs still seal, evidence is still recorded, scheduling is untouched (NFR-GPU-055). For S3 the attempt stays `REBUILD_FAILED` with a clear message and no stack trace (FR-STW-010, NFR-AVAIL-001).

### Data (ER-03)

`FAULT_EVIDENCE`(node, unit, job, run, owner_lab, exit_code, failure_category, at, consumed_at; **no** log bodies/metrics/project name) · `HARDWARE_WARNING`(unit_signature, state, close_reason, steward_finding json) · `DIAGNOSIS`(run_id, agent_id, agent_version, at) · `PROPOSAL`(diagnosis_id, state PROPOSED/APPROVED/REJECTED, gpu_hours) · `STEWARD_LINK`(token_hash, attempt_id, user_id, session_id, expires_at, consumed_at) · `RAG_CHUNK`(user_id, attempt_id, embedding) + caches + prompt history, all in `steward_private`.

## 5. Config (§16.2, §16.4)

| Parameter | Default |
|---|---|
| `correlation_window` | 24 h (flagged "needs calibration") |
| `min_distinct_users_for_warning` | 2 |
| `warning_cooldown` | 24 h |
| `max_evidence_bundle_rows` | 50 |
| `steward_link_ttl` | 15 min (must be > 0) |
| `rag_retention_period` | 90 days (must be > 0); also purged on account deactivation; audit records are exempt |

## 6. Full requirement checklist

- **S1/S2:** FR-GPU-146–153, 155 (dashboard shows pending proposals), 158 (admin warning view), 159–167, 187; NFR-GPU-048, 055, 056, 057
- **S3/S4:** FR-STW-001–018
- **Isolation/security:** NFR-SEC-001–008, NFR-AUTH-18 (admin permission set has no access to narratives, RAG responses or prompts), FR-AUTH-73/74 (Steward service principal), FR-ML-005 (admin sees RAG pipeline *health* only), FR-INT-05/07
- **Cross-cutting:** FR-INT-01, FR-INT-06 (one append-only audit log), NFR-PERF-001, NFR-AVAIL-001, **NFR-INT-05** (all four contexts testable with a stub, no model)
- **Use cases:** UC-GPU-20, UC-GPU-21, UC-STW-01–03. **User stories:** US-STW-01–03, US-GPU-12, US-ADM-05

## 7. Acceptance tests to target

- **S3/S4 (ML SRS):** A01 first-run gets no link · A02 rebuild fail gets a link · A03 cross-user RAG returns zero rows · A05/A06 optimize allowed / blocked · A07 web query sanitised · A08 commands are text only · A09 expired · A10 replay · A11 user mismatch · A12 rejected means no Steward · A13 retry isolation · A14 concurrent attempts · A15 cancel · A16 link-gen failure · A17 invalid session · A18 audit without payload · A19 prompt injection in log · A20 search down · A21 empty RAG · A22 backend down · A23 secret redaction · A24 retention expiry · A25 cache isolation · A26/A27 optimize data boundary / ownership · A29 error message content · A31 admin privacy · A32 post-init artifact failure · A33 provenance labels · A35 diagnostics purged on deactivation.
- **S1/S2 (GPU SRS):** A-21/A-22 (no bypass of approval, no writes), A-33/A-36 (output confidentiality), A-37 (scheduling independent of Steward), A-66 (diagnosis immutable), A-71 (job output treated as data).
- **Live demo (§20.2):** (3) run fails → cited diagnosis → approve fix → corrected job queued; (4) three users fail on one machine → warning → admin freezes; (6) job output containing agent instructions is treated as data and flagged.
- **Quality evaluation (Project Doc):** hand-label ~100 real failed runs; report accuracy with a confusion matrix across classes, plus proposal approve/reject counts. Report honestly if the numbers are bad.

## 8. Gaps and contradictions in the specs (decide these before building)

> Resolved in this repo by ADR-S01 (gaps 1 and 6), ADR-S03 (gap 2), ADR-S05 (gap 3), ADR-S04 (gap 4) and ADR-S08 (gaps 5, 7 and 8). Items still marked *open* in ADR-S08 are tracked as issues.

1. **RAG ingestion is a write.** SD-17 has the Runtime "sanitise then ingest" into the DB, yet the Runtime is supposed to hold only `SELECT` (SADD §8.2, FR-AUTH-73). **Suggested fix:** the server (Session Service) runs Sanitiser + Ingestor and writes `RAG_CHUNK`; the Runtime only retrieves. The same applies to prompt history and response caches.
2. **The Claude API is itself an external service.** §14.4 says never send code, secrets or PII to an external service, but S1 reads log content and sends it to an LLM. Only web search is explicitly covered by sanitisation (FR-STW-012). Either sanitise before **every** LLM call (S1 included) or record a decision that the LLM provider is a trusted processor. This needs a team or instructor decision.
3. **S2 can't do the similarity judgement it's justified by.** The rationale is that "failure text differs in wording", but the bundle has no log bodies, only exit code and failure category (NFR-GPU-056). As specified, S2 is close to a rule. Options: accept that S2 is mostly advisory (ADR-21 already makes the server threshold authoritative), or add a sanitised, non-content failure signature field to evidence rows.
4. **No output schema.** `StructuredResult` is never defined, yet citation stripping (FR-GPU-161) needs machine-checkable claims. Define a per-context JSON schema (claims[] each with `cites: [record_id]`), validate in the Coordinator, and reject malformed output.
5. **"Permitted metrics" for S4** is undefined. Pick a list.
6. **S1 vs Project Doc identity:** the Project Doc says tool calls run "with the invoking person's permissions"; the SADD narrows this to a read-only scoped token (ADR-18). Go with the SADD.
7. **S3 + S1 on the same failure** produce two diagnoses. Decide whether the S3 session should show or cite the S1 diagnosis.
8. **Classification label set**: the five categories come from the Project Doc only and aren't in a requirement. Freeze them for the evaluation.

## 9. Suggested build order

1. `ReasoningPort` + `StubReasoning` + output schemas (unblocks every test without a model; NFR-INT-05).
2. Seal-time hooks: failure event, fault-evidence row, `steward_tasks` outbox; Coordinator loop with retry/backoff.
3. Tool Gateway with scoped tokens and refusal auditing; read-only DB role for the Runtime.
4. S1 end to end: citation validator, diagnosis/finding/proposal persistence, approval through the normal submission path, dashboard view.
5. Warning Service (deterministic) + S2 advisory + admin warning view.
6. `steward_private` schema with RLS, link table and guarded consume, Session Service.
7. Sanitiser (with an A23 test corpus), Ingestor, Retriever, Prompt Assembler, Output Guard, web-search adapter with allow-listed queries.
8. S3 web UI, then S4 `prayog steward optimize`.
9. Retention purge job; prompt-injection, isolation and unavailability test suites; the 100-failure evaluation.
