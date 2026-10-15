import unittest

from schemas.mlflow import ProfessorRunOut, _redact_sensitive_metadata, _redact_sensitive_text


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


if __name__ == "__main__":
    unittest.main()
