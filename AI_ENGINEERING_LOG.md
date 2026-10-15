# AI Engineering Log — Architecture and ML Flow

## Scope and evidence

This log records AI-assisted engineering decisions for the ML Flow lifecycle and Steward handoff. The prompt/context entries below summarize the task briefs and acceptance criteria used during implementation; they are not presented as verbatim transcripts. The linked pull requests and CI runs are the evidence for implementation and evaluation. Append exact prompt/response excerpts when the team retains them.

## Evaluated recommendations

### 1. Make the sealed-run event transactional and idempotent

- **Prompt/context:** Implement the experiment-ledger handoff so a successfully recorded snapshot emits a stable event, without copying artifact contents into the event.
- **AI recommendation:** Write the `RUN_SEALED` outbox event in the same database transaction as the snapshot, revalidate the persisted run/snapshot before dispatch, and deduplicate ledger consumption with a database uniqueness constraint on `event_key`.
- **Output:** [PR #6 — Run-sealed outbox and idempotent ledger consumer](https://github.com/baymaxisme/Prayog/pull/6).
- **Evaluation:** The first CI run exposed a test defect: the test inspected the last `db.add` call and received the outbox event rather than the snapshot. The test was corrected to select the persisted-snapshot object by its state and run ID. The subsequent full validation run passed, including clean PostgreSQL migrations, lifecycle tests, Docker worker smoke tests, and the authenticated API-to-worker test ([run #334](https://github.com/baymaxisme/Prayog/actions/runs/38680938463)).
- **Decision:** Accepted, with the regression test fixed. The downstream ledger consumer remains an injected integration, not a claim that a production AI service is configured.

### 2. Enforce the rebuild pre-initiation privacy boundary

- **Prompt/context:** A rebuild rejected before it is created must not reveal whether a snapshot exists or create a rebuild attempt.
- **AI recommendation:** Normalize missing, foreign-owned, non-recorded, and artifact-integrity failures to the same generic `REBUILD_REJECTED` response; create no Attempt ID or outbox event until validation succeeds.
- **Output:** [PR #10 — Non-disclosing rebuild rejection](https://github.com/baymaxisme/Prayog/pull/10).
- **Evaluation:** The validation workflow passed on the PR head, including lifecycle tests and end-to-end worker checks ([run #319](https://github.com/baymaxisme/Prayog/actions/runs/38596465251)).
- **Decision:** Accepted. After initiation, failures follow the ordinary persisted attempt lifecycle; the pre-initiation response stays generic.

### 3. Scope Steward retrieval by authenticated user and failed attempt

- **Prompt/context:** Add context retrieval for diagnosis without allowing cross-user retrieval, arbitrary attempt lookup, or untrusted content to become an instruction to mutate code.
- **AI recommendation:** Require both the authenticated user ID and rebuild-attempt ID; ingest only explicitly sanitized chunks attached to an owned `REBUILD_FAILED` attempt; default-deny when either scope is missing.
- **Output:** [PR #12 — Attempt-scoped Steward retrieval context](https://github.com/baymaxisme/Prayog/pull/12).
- **Evaluation:** Tests cover absent scope, foreign attempts, sanitization gating, and embedding validation. Full validation passed ([run #327](https://github.com/baymaxisme/Prayog/actions/runs/38598637894)).
- **Decision:** Accepted. Retrieval is scoped in the database query before chunk content is loaded; the agent is not granted write or execution authority.

### 4. Bind diagnostic links to sessions and audit security outcomes

- **Prompt/context:** Diagnostic logs are sensitive and must not become a durable bearer capability or be exposed through ordinary audit records.
- **AI recommendation:** Bind each single-use, expiring diagnostic link to the authenticated owner session; audit creation and allow/deny outcomes without recording raw tokens or log contents.
- **Output:** [PR #13 — Session-bound diagnostic links and audit outcomes](https://github.com/baymaxisme/Prayog/pull/13).
- **Evaluation:** Regression tests cover creation denial, successful consumption, session mismatch, replay, invalid TTL, and audit behavior. The latest validation run passed, including clean migrations, lifecycle tests, Docker worker smoke tests, and API-to-worker end-to-end checks ([run #332](https://github.com/baymaxisme/Prayog/actions/runs/38599040083)).
- **Decision:** Accepted. Existing links are backfilled fail-closed, and link consumption requires authentication and the same session.

### 5. Limit professor ledger access to assigned students and metadata

- **Prompt/context:** Professors need a read-only view of enrolled students' recorded experiment snapshots, but must not gain access to Steward sessions or RAG state.
- **AI recommendation:** Resolve the professor profile from the authenticated user, scope the query to currently approved students assigned to that professor, and verify the matching persisted run and recorded snapshot before returning metadata.
- **Output:** [PR #14 — Professor-scoped read-only experiment ledger](https://github.com/baymaxisme/Prayog/pull/14).
- **Evaluation:** Added tests for role enforcement, returned metadata, and fail-closed handling when the ledger entry does not match a persisted snapshot. Full validation passed, including clean PostgreSQL migrations, Alembic check, lifecycle tests, Docker worker smoke tests, and authenticated API-to-worker end-to-end test ([run #336](https://github.com/baymaxisme/Prayog/actions/runs/38681071350)).
- **Decision:** Accepted after CI validation. The PR remains open for review.

## Rejected alternatives and safety rationale

1. **Rejected:** Trusting a client-supplied event name or process exit code as proof of success. **Reason:** Eligibility must follow the persisted lifecycle state and verified artifacts.
2. **Rejected:** Including source archives, weights, raw diagnostic logs, or bearer tokens in outbox payloads and audit records. **Reason:** These payloads can outlive the request and broaden the exposure surface.
3. **Rejected:** Allowing unscoped Steward retrieval when identity or attempt context is absent. **Reason:** Missing authorization context must fail closed, not widen access.
4. **Rejected:** Allowing Steward to patch or execute submitted code. **Reason:** Diagnosis is read-only; any proposed change must be a separate reviewable output with explicit user adoption.

## Open limitations

- PRs are proposals and remain subject to review; no Jira issue is marked Done by this log.
- Production Steward and optimization transports and service-to-service authorization are not configured.
- The entries above summarize the engineering prompts/context. Exact verbatim model transcripts should be added if required by the course's evidence standard.
