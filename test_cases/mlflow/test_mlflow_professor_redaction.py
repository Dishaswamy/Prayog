import unittest

from schemas.mlflow.experiments import ProfessorRunOut, _redact_sensitive_metadata, _redact_sensitive_text


class ProfessorMetadataRedactionTests(unittest.TestCase):
    def test_nested_metadata_redacts_secret_keys_without_changing_safe_values(self):
        metadata = {
            "learning_rate": 0.01,
            "AWS_SECRET_ACCESS_KEY": "private-value",
            "nested": [
                {"client_secret": "another-private-value", "epochs": 4},
                {"Authorization": "Bearer abc.def.ghi"},
            ],
        }
        result = _redact_sensitive_metadata(metadata)
        self.assertEqual(result["learning_rate"], 0.01)
        self.assertEqual(result["AWS_SECRET_ACCESS_KEY"], "[REDACTED]")
        self.assertEqual(result["nested"][0]["client_secret"], "[REDACTED]")
        self.assertEqual(result["nested"][0]["epochs"], 4)
        self.assertEqual(result["nested"][1]["Authorization"], "[REDACTED]")

    def test_sensitive_text_redacts_assignment_bearer_and_url_credentials(self):
        text = (
            "DB_PASSWORD=private123 "
            "Authorization: Bearer abc.def.ghi "
            "https://user:password@example.test/path"
        )
        result = _redact_sensitive_text(text)
        self.assertNotIn("private123", result)
        self.assertNotIn("abc.def.ghi", result)
        self.assertNotIn("user:password", result)
        self.assertIn("[REDACTED]", result)

    def test_metadata_redaction_handles_serialized_credentials_recursively(self):
        result = _redact_sensitive_metadata({
            "notes": "api_key=do-not-share",
            "items": ["access_token: hidden", "ordinary note"],
        })
        self.assertNotIn("do-not-share", result["notes"])
        self.assertNotIn("hidden", result["items"][0])
        self.assertEqual(result["items"][1], "ordinary note")

    def test_professor_response_models_apply_redaction(self):
        run = ProfessorRunOut.model_validate({
            "id": 1, "experiment_id": 2, "project_id": "p", "model_id": "m",
            "run_type": "FIRST_TIME", "source_snapshot_id": None, "status": "FAILED",
            "logging_state": "DEGRADED", "logging_warnings": ["token=private"],
            "hyperparameters": {"learning_rate": 0.1, "api_key": "private"},
            "environment": {"python": "3.12", "nested": {"password": "private"}},
            "code_snapshot_sha256": None, "code_artifact_id": None,
            "weights_artifact_id": None, "logs_artifact_id": None,
            "error_summary": "failed with access_token=private",
            "created_at": "2026-01-01T00:00:00", "started_at": None,
            "finished_at": None, "updated_at": "2026-01-01T00:00:00",
        })
        self.assertEqual(run.hyperparameters["api_key"], "[REDACTED]")
        self.assertEqual(run.environment["nested"]["password"], "[REDACTED]")
        self.assertNotIn("private", run.error_summary)
        self.assertNotIn("private", run.logging_warnings[0])


    def test_redaction_covers_hyphenated_and_prefixed_credential_names(self):
        result = _redact_sensitive_text(
            "service-api-key=abc DB-ACCESS-TOKEN:xyz oauth_client_secret=hidden"
        )
        self.assertNotIn("abc", result)
        self.assertNotIn("xyz", result)
        self.assertNotIn("hidden", result)
        self.assertEqual(result.count("[REDACTED]"), 3)

    def test_redaction_preserves_safe_assignments_and_numeric_metadata(self):
        metadata = {
            "epochs": 12,
            "learning-rate": 0.01,
            "description": "training_mode=distributed",
            "nested": [True, None, 3.5],
        }
        result = _redact_sensitive_metadata(metadata)
        self.assertEqual(result, metadata)

    def test_sensitive_key_matching_is_case_and_separator_insensitive(self):
        result = _redact_sensitive_metadata({
            "API-KEY": "one",
            "refresh_token": "two",
            "PrivateKey": "three",
            "ordinary": {"SESSION-CREDENTIAL": "four"},
        })
        self.assertEqual(result["API-KEY"], "[REDACTED]")
        self.assertEqual(result["refresh_token"], "[REDACTED]")
        self.assertEqual(result["PrivateKey"], "[REDACTED]")
        self.assertEqual(result["ordinary"]["SESSION-CREDENTIAL"], "[REDACTED]")

    def test_redaction_handles_empty_and_non_string_values(self):
        self.assertEqual(_redact_sensitive_text(""), "")
        self.assertIsNone(_redact_sensitive_text(None))
        self.assertEqual(_redact_sensitive_metadata(None), None)
        self.assertEqual(_redact_sensitive_metadata(42), 42)

    def test_url_redaction_preserves_scheme_and_host(self):
        result = _redact_sensitive_text("connect=mysql://user:pass@db.example.test:3306/app")
        self.assertIn("mysql://[REDACTED]@db.example.test:3306/app", result)
        self.assertNotIn("user:pass", result)

if __name__ == "__main__":
    unittest.main()
