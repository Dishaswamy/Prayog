# Traceability: requirement → code → test

`tools/check_trace.py` runs in CI and fails if a test named here does not exist. Status: **done** = implemented and tested, **partial** = mechanism in place but the end-to-end flow comes in a later phase, **todo** = phase shown.

| Requirement | What it demands | Code | Tests | Status |
|---|---|---|---|---|
| ID-05 | One Steward, four contexts | `steward_contracts.Context`, `envelope.RESULT_TYPES` | `test_inputs_round_trip_through_the_discriminated_union` | done |
| FR-GPU-151, FR-AUTH-73 | Steward never writes; write attempts refused and audited | `steward_runtime` (no DB, lint-imports), `coordinator.validation` | `test_nfr_int_05_write_attempt_is_rejected_and_flagged` | partial (audit: Phase 2) |
| NFR-INT-05 | Four contexts testable with a stub: fixed, malformed, uncited, write | `steward_runtime.stub` | `test_nfr_int_05_valid_stub_response_passes`, `test_nfr_int_05_malformed_response_is_rejected`, `test_nfr_int_05_uncited_claims_are_stripped` | done |
| FR-GPU-161, FR-STW-018 | Uncited claims stripped before display; no fabricated evidence | `coordinator.validation` | `test_fr_gpu_161_claims_citing_unknown_or_no_records_are_stripped`, `test_fr_stw_018_evidence_found_is_withdrawn_when_no_internal_claim_survives` | done |
| FR-GPU-148 | S1 reads only its run and the same project's earlier runs | `ports.RunReader`, `coordinator.validation` | `test_fr_gpu_148_earlier_runs_are_same_project_only_and_newest_first`, `test_fr_gpu_148_matching_failures_must_be_earlier_runs_of_this_invocation` | partial (input assembly: Phase 2) |
| FR-GPU-149, 187 | Proposal needs approval; owner sees exact change and GPU-hours | `contracts.ProposalDraft`, `ports.JobSpec.requested_gpu_hours` | `test_proposal_changes_need_cited_reasons_and_empty_proposal_is_dropped`, `test_proposal_cannot_touch_fields_outside_the_allow_list`, `test_requested_gpu_hours_is_gpus_times_walltime` | partial (approval flow: Phase 2) |
| FR-GPU-166, NFR-AUTH-18 | Admin sees the hardware finding only, no project content | `contracts.HardwareFinding` (enums only) | `test_fr_gpu_166_hardware_finding_has_no_free_text` | partial (admin API: Phase 3) |
| FR-GPU-165, NFR-GPU-056 | S2 reasons only over the metadata bundle | `contracts.EvidenceRow`, `coordinator.validation` | `test_fr_gpu_165_s2_rationale_and_rows_limited_to_the_bundle` | partial (bundle builder: Phase 3) |
| FR-STW-017 | Internal vs external provenance kept apart | `contracts.Provenance`, `coordinator.validation` | `test_fr_stw_017_provenance_must_match_the_cited_records` | done |
| FR-STW-016, NFR-GPU-048 | Job output and retrieved text are data, never instructions | `steward_runtime.prompts` | `test_system_prompt_is_frozen_and_cached_and_input_is_wrapped_as_data` | partial (injection corpus: Phase 5) |
| FR-STW-010, NFR-GPU-055 | Model down → clear unavailable state, nothing else blocked | `steward_runtime.claude` | `test_fr_stw_010_api_down_is_unavailable_without_internal_detail`, `test_non_ok_outcomes_pass_through_without_result` | partial (retries: Phase 2) |
| FR-GPU-160, NFR-GPU-057 | Diagnosis linked to run, agent identity and version; immutable | `db` migration 0001, `prompts.PROMPT_VERSION` | `test_ok_reply_becomes_contract_dict_with_versioned_agent`, `test_append_only_guarantees_are_installed`, `test_nfr_gpu_057_diagnoses_are_append_only` | partial (writer: Phase 2) |
| FR-GPU-163, 164 | Warnings closed only with a reason; one open per unit | `db` migration 0001 | `test_fr_gpu_164_one_open_warning_per_unit_signature`, `test_fr_gpu_164_only_one_open_warning_per_unit`, `test_fr_gpu_163_closing_a_warning_needs_a_reason` | partial (service: Phase 3) |
| NFR-SEC-001, 002 (A03) | Immutable tenant ID; DB-level user filter; default-deny | `db` migration 0001 (RLS) | `test_nfr_sec_002_every_private_table_forces_rls_default_deny`, `test_a03_nfr_sec_002_each_user_sees_only_their_rows`, `test_a03_missing_identity_default_denies`, `test_cannot_write_rows_for_another_user`, `test_nfr_sec_001_owner_and_attempt_are_immutable` | partial (retriever: Phase 4) |
| §16.2, §16.4, §16.7 | Config defaults and invariants | `prayog_steward.config` | `test_defaults_match_integrated_spec_section_16`, `test_section_16_7_invariants_are_enforced` | done |
| FR-GPU-146, 147, 150, 152, 153, 159, 167 | S1 pipeline, approval and audit | — | — | todo (Phase 2) |
| FR-GPU-158, 162 | Warning threshold and admin view | — | — | todo (Phase 3) |
| FR-STW-001…005, 011, 012, NFR-SEC-003, 004, 006, 007 | Links, sanitiser, caches, security audit | — | — | todo (Phase 4) |
| FR-STW-013, 014, 015 | S3 session, manual steps, isolation filter on retrieval | — | — | todo (Phase 5) |
| FR-STW-006, 007 | S4 optimize; nothing after cancellation | — | — | todo (Phase 6) |
| NFR-SEC-005, 008, NFR-PERF-001 | Professor boundary, retention purge, inference off the GPU pool | — | — | todo (Phase 7) |
