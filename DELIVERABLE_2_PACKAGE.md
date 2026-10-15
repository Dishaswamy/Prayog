# Deliverable 2 — Repository, Agentic AI, and Workflow Package

## Package status

This index brings together the repository, API documentation, ML Flow/Steward architecture, implementation-to-test traceability, and a repeatable workflow demonstration. It does **not** fabricate peer reviews, Jira sprint history, or a recorded demo. Those external evidence items are listed as pending and must be attached before the deliverable is considered complete.

## Repository and architecture

- **Repository:** [Prayog](https://github.com/baymaxisme/Prayog)
- **Setup and project overview:** [README](README.md)
- **ML Flow lifecycle and API usage:** [backend/ML_FLOW.md](backend/ML_FLOW.md)
- **Steward/optimization integration contract and safety gates:** [backend/AI_INTEGRATION.md](backend/AI_INTEGRATION.md)
- **AI-assisted design decisions and evaluation log:** [AI_ENGINEERING_LOG.md](AI_ENGINEERING_LOG.md) (proposed in PR #15; not yet merged)
- **API schema:** Start the backend locally and open `/docs` as described in the README.

### Agentic AI boundaries

The current repository provides a guarded handoff contract, not a configured production Steward or optimization service. The implemented boundaries are:

1. Steward diagnosis is eligible only for a persisted `REBUILD_FAILED` attempt with a verified log artifact.
2. First-time run failures, initiated/running rebuilds, cancelled rebuilds, and pre-initiation rejections are not eligible for diagnosis.
3. Steward receives no write or code-execution authority. Suggestions must be reviewable outputs and cannot silently patch user code.
4. Optimization is eligible only after a persisted `REBUILD_SUCCESS`; it cannot overwrite the source snapshot.
5. Outbox messages contain identifiers and bounded metadata, not source archives, model weights, raw logs, or bearer tokens.
6. Production service-to-service authentication, transport configuration, and live AI consumers remain deployment/integration work.

## Implementation traceability checkpoint

The following is a focused ML Flow traceability slice. CI links provide reproducible validation evidence for each implementation PR. PRs are still proposals until reviewed and merged.

| Requirement / acceptance point | Issue and implementation | Validation evidence |
|---|---|---|
| Capture the current worktree, lockfiles, configuration, seeds, and data fingerprints; reuse existing chunks and verify materialization | [PRAYOG-41 / PR #21](https://github.com/baymaxisme/Prayog/pull/21) | [CI run #397](https://github.com/baymaxisme/Prayog/actions/runs/38683235040)
| Store artifacts under scoped content-derived addresses and reuse identical uploads | [PRAYOG-66 / PR #18](https://github.com/baymaxisme/Prayog/pull/18) | [CI run #345](https://github.com/baymaxisme/Prayog/actions/runs/38681930659) |
| Synchronize run outputs into `results/<run_id>/` without changing identical local files | [PRAYOG-67 / PR #17](https://github.com/baymaxisme/Prayog/pull/17) | [CI run #348](https://github.com/baymaxisme/Prayog/actions/runs/38682006531) |
| Stream metrics during a run; reject duplicates; verify all supplied artifacts before sealing | [PRAYOG-79 / PR #5](https://github.com/baymaxisme/Prayog/pull/5) | [CI run #5](https://github.com/baymaxisme/Prayog/actions/runs/38552074458) |
| Preserve a queryable incomplete snapshot and prevent rebuilds from it | [PRAYOG-81 / PR #2](https://github.com/baymaxisme/Prayog/pull/2) | [CI run #2](https://github.com/baymaxisme/Prayog/actions/runs/38550463669) |
| Emit a stable run-sealed event and consume ledger events idempotently | [PRAYOG-80 / PR #6](https://github.com/baymaxisme/Prayog/pull/6) | [CI run #334](https://github.com/baymaxisme/Prayog/actions/runs/38680938463) |
| Admin can see aggregate volume/status but not project content | [PRAYOG-83 / PR #7](https://github.com/baymaxisme/Prayog/pull/7) | [CI run #329](https://github.com/baymaxisme/Prayog/actions/runs/38598687072) |
| Project IP handover verifies artifacts and preserves in-flight actor context | [PRAYOG-84 / PR #8](https://github.com/baymaxisme/Prayog/pull/8) | [CI run #317](https://github.com/baymaxisme/Prayog/actions/runs/38595996607) |
| Rebuild CLI waits for a terminal result and returns failure status correctly | [PRAYOG-85 / PR #9](https://github.com/baymaxisme/Prayog/pull/9) | [CI run #318](https://github.com/baymaxisme/Prayog/actions/runs/38596269295) |
| Pre-init rejection does not disclose snapshot existence or create an Attempt ID | [PRAYOG-86 / PR #10](https://github.com/baymaxisme/Prayog/pull/10) | [CI run #319](https://github.com/baymaxisme/Prayog/actions/runs/38596465251) |
| Rebuilds and first-time runs share timestamp-based queue ordering | [PRAYOG-87 / PR #11](https://github.com/baymaxisme/Prayog/pull/11) | [CI run #320](https://github.com/baymaxisme/Prayog/actions/runs/38596812376) |
| Steward context is scoped to an owned failed attempt and sanitized content | [PRAYOG-89 / PR #12](https://github.com/baymaxisme/Prayog/pull/12) | [CI run #327](https://github.com/baymaxisme/Prayog/actions/runs/38598637894) |
| Diagnostic links are session-bound, single-use, and audited | [PRAYOG-90 / PR #13](https://github.com/baymaxisme/Prayog/pull/13) | [CI run #332](https://github.com/baymaxisme/Prayog/actions/runs/38599040083) |
| Professors see only metadata for their currently assigned students | [PRAYOG-82 / PR #14](https://github.com/baymaxisme/Prayog/pull/14) | [CI run #336](https://github.com/baymaxisme/Prayog/actions/runs/38681071350) |

## Workflow demonstration runbook

Use a local test environment and test accounts; never commit secrets or paste bearer tokens into logs.

1. Follow the README to start PostgreSQL, install backend dependencies, apply Alembic migrations, and launch FastAPI.
2. Open `/docs` and authenticate as a test student or researcher.
3. Create a project, model, and experiment; create a run and start it with a verified code artifact.
4. Upload the code artifact and optional weights/log artifacts. Record individual metrics and a batch of metrics while the run is `RUNNING`.
5. Finish the run. Verify that the run reaches a terminal state and that a snapshot becomes `RECORDED` only when required and supplied artifacts pass integrity checks. Exercise a missing/corrupt artifact case and confirm degraded/incomplete behavior.
6. List the experiment snapshots and inspect the recorded metadata. Attempt to rebuild an incomplete or foreign-owned snapshot and confirm generic `REBUILD_REJECTED` without an Attempt ID.
7. Rebuild a valid recorded snapshot through the CLI or API. Poll until a terminal rebuild state and compare the CLI exit code with the persisted result.
8. For a failed rebuild with a verified log, exercise diagnostic-link creation and consumption. Confirm expiry, single use, session binding, and audit behavior; do not show raw tokens or logs on screen.
9. Inspect outbox state and demonstrate duplicate delivery/idempotency using the test suite or a local configured test handler. Do not imply that production Steward or optimization services are configured.
10. Authenticate as the supervising Professor and call `GET /mlflow/professor/ledger`. Confirm that only currently assigned students' matching `RECORDED` snapshots appear and that hyperparameters, artifacts, logs, Steward sessions, and RAG state are absent.

The automated CI workflow already covers clean PostgreSQL migrations, lifecycle tests, Docker worker smoke tests, and the authenticated API-to-worker end-to-end test. A human-recorded demonstration should capture the above scenarios and link its recording here.

## Implementation blockers and scope boundaries

- **PRAYOG-78 — Hardware fault investigation:** The current repository contains the ML Flow backend but no node, GPU-unit, heartbeat, allocation, or hardware-history model to query. Implementing freeze/drain/dismiss here would invent a parallel scheduler rather than integrate with the team's infrastructure component.
- **PRAYOG-88 — Metric comparator:** The imported acceptance criteria explicitly mark it deferred and blocked on CR-18. Do not implement comparison semantics until that change request is resolved.
- **PRAYOG-102 — Whole-chain simulated-pool suite:** [PR #20](https://github.com/baymaxisme/Prayog/pull/20) adds and passes the ML Flow first-time-run → snapshot → queued rebuild → worker execution end-to-end path ([CI run #394](https://github.com/baymaxisme/Prayog/actions/runs/38683213454)). The required simulated GPU pool init-to-release scheduler is not present in this repository, so the full pool acceptance remains blocked on the platform/scheduler component.

## Evidence still required before final submission

- [ ] **Jira sprint-board history:** attach an export or screenshots showing sprint/backlog history and link the actual Jira board. The board's workspace URL/history is not embedded in this repository, so no history is invented here.
- [ ] **Peer reviews:** obtain and retain peer review submissions/comments on the implementation PRs. Opening a PR is not itself a peer review.
- [ ] **Workflow demo recording:** record the runbook above in a clean environment and attach the recording URL.
- [ ] **Final traceability sign-off:** reconcile this ML Flow slice with the team-owned master traceability matrix and verify issue → PR → test → result after PR review/merge.
- [ ] **Verbatim AI evidence, if required:** append exact prompt/response excerpts from the team's retained transcripts. The AI Engineering Log currently labels its prompt descriptions as summaries rather than verbatim transcripts.

## Review and status policy

No Jira issue is marked Done by this package. Keep each issue open until the linked PR is reviewed, merged, and its acceptance criteria are satisfied. A passing CI run validates the tested code path; it does not replace peer review, deployment verification, or the external evidence items above.
