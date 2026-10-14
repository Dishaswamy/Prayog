# Traceability: requirement → code → test

`tools/check_trace.py` runs in CI and fails if a test named here does not exist. Integration tests run against real PostgreSQL with RLS enforced on a non-superuser role. **Done** means implemented and covered by the named tests. **Partial** means the rest belongs to another sub-team or is noted.

## S1: run-failure diagnosis

| Requirement | Code | Tests | Status |
|---|---|---|---|
| FR-GPU-146 failure event, evidence, S1 trigger | `hooks.on_run_sealed`, `coordinator.tasks` | `test_fr_gpu_146_failed_run_records_event_evidence_and_task`, `test_sealing_twice_is_idempotent` | done |
| FR-GPU-147 non-failures do not trigger; NODE_FAILURE is evidence | `hooks` | `test_fr_gpu_147_non_failures_trigger_nothing`, `test_fr_gpu_147_node_failure_is_evidence_without_diagnosis` | done |
| FR-GPU-148 reads only the run and its project's earlier runs | `gateway.Gateway.s1_request` | `test_adr_s03_model_input_is_sanitised_and_scoped`, `test_fr_gpu_148_earlier_runs_are_same_project_only_and_newest_first`, `test_fr_gpu_148_matching_failures_must_be_earlier_runs_of_this_invocation` | done |
| FR-GPU-149, 150, 187 approval-gated proposal via ordinary submission, exact change and GPU-hours shown | `coordinator.proposals`, `owner.OwnerService.approve` | `test_s1_end_to_end_cited_diagnosis_finding_and_bounded_proposal`, `test_uc_gpu_20_owner_approves_and_job_goes_through_ordinary_submission`, `test_proposal_cannot_touch_fields_outside_the_allow_list`, `test_submission_refusal_keeps_the_proposal_open`, `test_s1_diagnosis_and_approval_over_http` | done |
| FR-GPU-151, FR-AUTH-73 Steward never writes; refusals audited | `steward_runtime` (no DB; `lint-imports`), `coordinator.validation`, `api.writer` | `test_fr_gpu_151_write_attempt_is_refused_and_audited`, `test_fr_gpu_151_service_identity_cannot_write_over_http`, `test_nfr_int_05_write_attempt_is_rejected_and_flagged` | done |
| FR-GPU-153 audit of diagnoses, proposals and decisions | `audit.Auditor` | `test_s1_end_to_end_cited_diagnosis_finding_and_bounded_proposal` | done |
| FR-GPU-155 pending proposals listed; approve/reject | `api` `/steward/proposals` | `test_s1_diagnosis_and_approval_over_http`, `test_only_the_owner_can_see_or_decide_a_proposal` | done |
| FR-GPU-159 evidence row: metadata only | `db` migration 0001, `hooks` | `test_fr_gpu_158_admin_view_is_metadata_only` | done |
| FR-GPU-160, NFR-GPU-057 diagnosis append-only, agent identity and version | `coordinator.service`, migration triggers, `prompts.PROMPT_VERSION` | `test_nfr_gpu_057_diagnoses_are_append_only`, `test_append_only_guarantees_are_installed`, `test_ok_reply_becomes_contract_dict_with_versioned_agent` | done |
| FR-GPU-161 uncited claims stripped before display | `coordinator.validation` | `test_fr_gpu_161_claims_citing_unknown_or_no_records_are_stripped`, `test_nfr_int_05_uncited_claims_are_stripped` | done |
| FR-GPU-166, NFR-AUTH-18 hardware finding separate; admin sees no content | `contracts.HardwareFinding` (enums only), `owner.diagnoses_for_run` | `test_fr_gpu_166_hardware_finding_has_no_free_text`, `test_diagnosis_visibility_owner_professor_admin` | done |
| FR-GPU-167 rejected proposals retained | migration trigger, `owner.reject` | `test_fr_gpu_167_rejected_proposal_is_retained` | done |
| ID-03 approval is a commitment operation | `owner.approve` | `test_id_03_approval_is_refused_after_authorization_expiry` | done |
| NFR-GPU-055, NFR-AVAIL-001 no dependency on Steward availability | `hooks` (no model call), `coordinator` retries, `reasoning_client` | `test_nfr_gpu_055_sealing_never_calls_the_model`, `test_nfr_avail_001_unavailable_model_retries_then_records_unavailable`, `test_nfr_gpu_055_runtime_problems_become_outcomes_not_exceptions`, `test_crashed_worker_lease_expires_and_task_is_reclaimed`, `test_retry_waits_for_backoff` | done |

## S2: fault correlation and hardware warnings

| Requirement | Code | Tests | Status |
|---|---|---|---|
| FR-GPU-162 server threshold: distinct users within window | `hardware.evaluate_unit` | `test_fr_gpu_162_two_distinct_users_on_one_unit_raise_open_warning_and_s2_task`, `test_one_users_repeated_failures_never_raise_a_warning`, `test_failures_outside_the_window_do_not_count`, `test_different_units_are_separate_incidents`, `test_concurrent_seals_cannot_both_miss_the_threshold` | done |
| FR-GPU-163 admin closes with reason; evidence consumed | `hardware.close_warning`, migration | `test_fr_gpu_163_close_requires_reason_and_consumes_evidence`, `test_fr_gpu_163_closing_a_warning_needs_a_reason`, `test_closed_warning_cannot_be_reopened` | done |
| FR-GPU-164 attach to OPEN; cooldown after dismissal | `hardware.evaluate_unit`, unique index | `test_fr_gpu_164_further_failures_attach_to_the_open_warning`, `test_fr_gpu_164_only_one_open_warning_per_unit`, `test_fr_gpu_164_one_open_warning_per_unit_signature`, `test_dismissal_starts_a_cooldown` | done |
| FR-GPU-165, NFR-GPU-056 metadata-only bundle, capped, rationale within it | `gateway.s2_request`, `coordinator.validation` | `test_fr_gpu_165_bundle_is_capped`, `test_fr_gpu_165_s2_rationale_and_rows_limited_to_the_bundle`, `test_s2_advice_is_attached_and_cites_only_bundle_rows` | done |
| FR-GPU-152 a warning never changes node state | no node write path exists | `test_fr_gpu_152_a_warning_never_touches_node_state` | done (freeze/drain are the GPU team's) |
| FR-GPU-156, 158 admin view: metadata, findings, allocation history | `hardware.warning_detail`, `api` admin routes | `test_fr_gpu_158_admin_view_is_metadata_only`, `test_admin_reviews_and_closes_a_warning_with_metadata_only`, `test_admin_routes_are_invisible_to_non_admins` | done |
| SADD ADR-21 S2 advisory; skipped if already closed | `coordinator._s2` | `test_s2_skips_a_warning_closed_before_the_worker_ran` | done |

## S3: interactive rebuild diagnosis

| Requirement | Code | Tests | Status |
|---|---|---|---|
| FR-STW-001, 002 only for REBUILD_FAILED; not for first runs, rejections or other states | `sessions.links.create` | `test_fr_stw_002_no_link_for_other_states_or_other_users` | done (flag classification is the CLI's) |
| FR-STW-003 failure message with IDs, reason, availability | `cli.report_rebuild_failure`, `api` link route | `test_fr_stw_003_rebuild_failure_message_includes_link_and_ids`, `test_a02_link_issued_for_failed_rebuild_with_cli_message` | done |
| FR-STW-004 single-use, 15 min, owner-bound, no insecure fallback | `sessions.links` | `test_a09_expired_link_is_denied`, `test_a10_link_is_single_use`, `test_a11_other_user_is_denied_before_any_context_is_exposed`, `test_link_dies_with_the_cli_session_that_requested_it`, `test_a16_link_failure_gives_no_insecure_fallback` | done |
| FR-STW-005, NFR-SEC-003 only the bound attempt; identity checked before context | `sessions.links.resolve`, `sessions.s3` | `test_a17_expired_or_missing_session_is_denied`, `test_logout_revokes_the_session` | done |
| FR-STW-010 backend down: state kept, clear message, no traces | `sessions.s3`, `api` error handlers | `test_fr_stw_010_backend_down_keeps_state_and_says_so`, `test_fr_stw_010_api_down_is_unavailable_without_internal_detail`, `test_security_headers_and_no_stack_traces` | done |
| FR-STW-011, 012, NFR-SEC-006 sanitise before RAG, LLM and search; allow-listed queries | `rag.sanitiser`, `search.build_query` | `test_a23_planted_secret_pii_and_code_are_removed`, `test_a23_diagnostic_content_survives`, `test_search_query_is_built_only_from_error_facts`, `test_s3_answer_cites_own_records_web_is_labelled_and_query_is_allow_listed` | done |
| FR-STW-013, 014 manual steps, never executed | `contracts.RemediationStep`, UI renders text only | `test_s3_answer_cites_own_records_web_is_labelled_and_query_is_allow_listed` | done |
| FR-STW-015, NFR-SEC-001, 002 user isolation enforced by the database | migration RLS, `db.engine.private_tx`, `sessions.context` | `test_a03_nfr_sec_002_each_user_sees_only_their_rows`, `test_a03_missing_identity_default_denies`, `test_cannot_write_rows_for_another_user`, `test_nfr_sec_001_owner_and_attempt_are_immutable`, `test_nfr_sec_002_rls_hides_other_users_chunks_even_by_attempt_id`, `test_app_role_cannot_bypass_rls`, `test_nfr_sec_002_every_private_table_forces_rls_default_deny` | done |
| NFR-SEC-004, A13 caches and context partitioned by user and attempt | `sessions.s3` cache key, `context.retrieve` | `test_nfr_sec_004_cache_serves_only_the_same_user_and_attempt`, `test_a13_other_users_attempt_cannot_be_included` | done |
| FR-STW-016, NFR-GPU-048 untrusted content is data | `steward_runtime.prompts`, UI uses `textContent` | `test_a19_injected_instructions_stay_inside_the_data`, `test_system_prompt_is_frozen_and_cached_and_input_is_wrapped_as_data` | done |
| FR-STW-017, 018 provenance kept; no fabricated evidence | `coordinator.validation`, `sessions.s3` notices | `test_fr_stw_017_provenance_must_match_the_cited_records`, `test_fr_stw_018_evidence_found_is_withdrawn_when_no_internal_claim_survives` | done |
| A20 web search failure degrades | `search.WebSearch` | `test_a20_search_failure_degrades_and_never_raises` | done |
| NFR-SEC-007 security events audited without payload | `audit.Auditor`, `sessions.links` | `test_a11_other_user_is_denied_before_any_context_is_exposed`, `test_optimize_is_refused_outside_its_preconditions` | done |

## S4: optimisation advisory

| Requirement | Code | Tests | Status |
|---|---|---|---|
| FR-STW-006, A05, A26 owner's REBUILD_SUCCESS only; own permitted data | `sessions.s4` | `test_a05_optimize_on_success_uses_only_own_permitted_data`, `test_s4_suggestions_need_citations` | done |
| A06, A15, A27, FR-STW-007 refused for FAILED/CANCELLED or another user | `sessions.s4` | `test_optimize_is_refused_outside_its_preconditions` | done |

## Knowledge base (ADR-S10)

| Requirement | Code | Tests | Status |
|---|---|---|---|
| Ported cleaning and chunking, with the notebook's defects fixed | `knowledge.text` | `test_references_are_cut_only_at_a_late_heading`, `test_hyphenation_is_repaired_only_across_line_breaks`, `test_captions_tables_page_numbers_and_arxiv_stamps_are_removed`, `test_word_chunks_overlap_and_drop_tiny_tails` | done |
| arXiv metadata and PDFs | `knowledge.arxiv` | `test_arxiv_feed_is_parsed_into_metadata` | done |
| Ingest: real PDFs, sanitised chunks, idempotent | `knowledge.store` | `test_pdfs_are_extracted_chunked_sanitised_and_stored`, `test_reingest_is_idempotent_and_changes_replace_chunks` | done |
| Corpus is read-only to the request path | migration 0002 | `test_application_role_can_read_but_never_change_the_corpus` | done |
| Retrieval ranks the relevant paper; diverse hits | `knowledge.index` | `test_index_ranks_the_relevant_paper_first` | done |
| FR-STW-017 literature kept apart from user records and the web | `coordinator.validation` | `test_literature_claims_must_rest_on_reference_records_only` | done |
| S3 and S4 cite papers; sources show title and link | `sessions.s3`, `sessions.s4` | `test_s3_answer_cites_papers_with_literature_provenance`, `test_s4_suggestions_cite_papers_and_list_them_as_sources`, `test_model_input_carries_reference_records`, `test_heuristic_uses_literature_and_passes_validation` | done |
| Corpus listing for signed-in users | `api` `/steward/knowledge/documents` | `test_documents_endpoint_lists_the_corpus` | done |

## Cross-cutting

| Requirement | Code | Tests | Status |
|---|---|---|---|
| ID-05 one subsystem, four contexts | `steward_contracts` | `test_inputs_round_trip_through_the_discriminated_union`, `test_payload_context_cannot_be_spoofed` | done |
| NFR-INT-05 four contexts testable with a stub | `steward_runtime.stub` | `test_nfr_int_05_valid_stub_response_passes`, `test_nfr_int_05_malformed_response_is_rejected` | done |
| ADR-S04 closed output contracts | `steward_contracts` | `test_result_schemas_forbid_additional_properties`, `test_extra_fields_are_rejected`, `test_request_shape_and_round_trip_for_every_context` | done |
| NFR-SEC-008, A24, A35 retention and deactivation purge | `retention` | `test_a24_retention_purge_removes_old_state`, `test_a35_deactivation_purges_personal_state_but_keeps_project_records` | done |
| NFR-SEC-005 no Professor access to S3 | S3 routes require the session's own user | `test_diagnosis_visibility_owner_professor_admin` | done |
| FR-ML-005 admin sees pipeline health as aggregates | `api` `/admin/health` | `test_fr_ml_005_admin_health_is_aggregate_only` | done |
| NFR-AUTH-17 non-enumerable identifiers | 404 for anything not visible | `test_only_the_owner_can_see_or_decide_a_proposal` | done |
| NFR-GPU-046 no stack traces in errors | `api.install_error_handlers` | `test_security_headers_and_no_stack_traces` | done |
| CR-H cookie auth needs a CSRF header | `api.writer` | `test_cookie_authenticated_writes_need_the_csrf_header` | done |
| §16.2, §16.4, §16.7 parameters and invariants | `config` | `test_defaults_match_integrated_spec_section_16`, `test_section_16_7_invariants_are_enforced` | done |
| NFR-PERF-001 inference off the GPU pool | Runtime is a separate CPU service calling the API; local embeddings | (deployment) | done by design |
| Project Document §10 evaluation | `tools/evaluate.py`, `steward_runtime.heuristic` | `test_heuristic_output_always_passes_the_server_contract` | partial: needs ~100 real labelled failures |
