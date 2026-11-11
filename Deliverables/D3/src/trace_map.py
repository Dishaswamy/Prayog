# Jira key -> (automated tests in the repository, other D3 evidence). Built by reading the test files.
T = {
 17: ([], "CI workflow `.github/workflows/mlflow-validation.yml`; branch protection on GitHub"),
 18: ([], "smart-commit keys in branch names (PRAYOG-79…92)"),
 19: ([], "CI: `alembic upgrade head` on empty PostgreSQL + `alembic check`; repeated locally 6 Nov"),
 20: ([], "config validated at startup (`load_config`); no unit test found"),
 21: ([], "no test found"),
 22: ([], "docker-compose.yml (postgres, steward-runtime); simulated agents used in drills"),
 23: ([], "`/metrics` exercised manually; no test found"),
 24: ([], "not verified (Deployment §9)"),
 25: (["test_auth_routes", "test_login"], ""),
 26: (["test_access_request_purpose"], ""),
 27: (["test_access_request_purpose"], ""),
 28: (["test_access_request_purpose"], ""),
 29: (["test_access_request_purpose"], ""),
 30: (["test_cli_login", "cli/test_login_logout"], ""),
 31: (["test_access_request_purpose", "test_lab_scheduling"], ""),
 32: (["test_gpu_agent_api"], "drill F1 (re-start with stored credential)"),
 33: (["test_review_fixes", "test_auth_routes", "cli/test_protected_commands"], "probes A01–A06, A11"),
 34: (["test_auth_routes", "test_review_fixes"], "probes A12–A13"),
 35: (["test_access_request_purpose", "test_cli_login"], ""),
 36: (["test_cli_login"], ""),
 37: (["test_review_fixes"], ""),
 38: ([], "no test found"),
 39: (["test_access_request_purpose", "test_auth_routes"], ""),
 40: ([], "no test found"),
 41: ([], "no test found"),
 42: (["test_review_fixes", "test_pool_picture", "test_admin_cancel_job"], ""),
 43: ([], "no test found"),
 44: (["cli/test_folder"], ""),
 45: (["cli/test_folder", "test_project_versions", "test_staging"], ""),
 46: (["test_gpu_run_submission"], ""),
 47: (["test_gpu_run_submission"], "probe A16"),
 48: (["test_gpu_run_submission"], "load test: POST /jobs p95 0.30 s"),
 49: (["test_pass_equivalence", "test_node_sharing"], ""),
 50: (["test_lab_scheduling"], ""),
 51: (["test_pass_equivalence", "test_lab_scheduling"], ""),
 52: (["test_pass_equivalence", "test_rebuild_scheduling_path"], "drill F4 (event-driven pass without the tick)"),
 53: (["test_lab_scheduling", "test_concurrency"], "concurrency C3, C5, C6"),
 54: (["test_job_list_batched", "test_web_app"], ""),
 55: (["test_admin_cancel_job"], "quota path only indirectly"),
 56: ([], "no test found"),
 57: ([], "no test found"),
 58: (["test_gpu_rebuild"], ""),
 59: (["test_gpu_agent_api"], ""),
 60: (["test_node_agent_units", "test_node_agent_docker"], "CI hardened-container step"),
 61: (["test_node_agent_runs"], ""),
 62: (["test_web_app", "test_job_list_batched"], "load test GET /jobs/{id}"),
 63: (["test_concurrency", "test_review_fixes"], "concurrency C5"),
 64: (["test_gpu_run_finalize", "test_review_fixes"], "drill F1"),
 65: (["test_lab_scheduling", "test_pool_picture"], ""),
 66: (["test_gpu_run_finalize"], ""),
 67: (["test_job_list_batched"], "planned maintenance stop in the job view"),
 68: ([], "no test found"),
 69: ([], "no test found"),
 70: (["test_gpu_run_finalize", "test_mlflow_artifacts"], ""),
 71: (["cli/test_runs"], ""),
 72: (["cli/test_runs"], ""),
 73: (["test_gpu_agent_api", "test_pool_picture"], ""),
 74: ([], "drill F1/F4: NODE_DOWN alerts raised; no automated test found"),
 75: (["test_pool_picture", "test_hardware_investigation"], ""),
 76: (["test_hardware_investigation"], ""),
 77: (["test_hardware_investigation"], ""),
 78: (["test_node_sharing"], ""),
 79: (["test_node_sharing"], ""),
 80: (["test_pool_picture"], ""),
 81: (["test_admin_cancel_job"], ""),
 82: (["test_hardware_investigation"], ""),
 83: (["test_mlflow_lifecycle", "test_mlflow_metric_batch"], ""),
 84: (["test_mlflow_outbox", "test_mlflow_ledger_consumer", "test_mlflow_outbox_worker"], ""),
 85: (["test_mlflow_lifecycle", "test_mlflow_snapshot_storage_failures"], ""),
 86: (["test_mlflow_professor", "test_mlflow_professor_api", "test_mlflow_professor_ledger", "test_mlflow_professor_postgres", "test_mlflow_professor_redaction"], ""),
 87: (["test_admin_mlflow"], ""),
 88: (["test_mlflow_handover", "test_review_fixes"], ""),
 89: (["test_gpu_rebuild", "test_mlflow_rebuild_preinit"], ""),
 90: (["test_mlflow_rebuild_preinit", "test_mlflow_lifecycle"], ""),
 91: (["test_rebuild_scheduling_path", "test_mlflow_queue_order"], ""),
 92: (["test_mlflow_compare"], ""),
 93: (["test_steward_context"], "Steward suite (outside `main`)"),
 94: (["test_diagnostic_session_audit", "test_cli_rebuild_reaches_steward"], "Steward suite"),
}
for k in range(95, 103):
    T[k] = (["tests_steward_check"], "Steward suite (`steward/tests`, kept outside `main` at the team lead's request)")
T[103] = (["test_concurrency", "test_job_list_batched", "test_database_unavailable"], "Test and Coverage reports")
T[104] = (["test_concurrency"], "Concurrency report")
T[105] = (["test_pass_equivalence"], "property-style equivalence of passes; no Hypothesis suite")
T[106] = (["test_mlflow_e2e", "test_cli_end_to_end"], "CI e2e step")
T[107] = ([], "Security report §7 (37 adversarial cases)")
T[108] = ([], "Security report")
T[109] = (["test_job_list_batched"], "Performance report")
T[110] = (["test_database_unavailable", "test_concurrency"], "Failure and Recovery report")
T[111] = ([], "Defect history (13 defects)")
T[112] = ([], "AI Engineering Log (13)")
T[113] = ([], "DELIVERABLE_2_PACKAGE.md in the repository")
T[114] = ([], "this matrix (11)")
T[115] = ([], "Costing (08), Gantt (09)")
T[116] = ([], "this D3 package (00–13)")
T[117] = ([], "Sprint 3")
T[118] = ([], "Project report (12)")
T[119] = ([], "this matrix (11), dashboard (12)")
