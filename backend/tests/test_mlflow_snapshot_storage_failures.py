from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from logic.mlflow import finish_run


class SnapshotStorageFailureTests(TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=7)
        self.run = SimpleNamespace(
            id=3,
            experiment_id=2,
            project_id="project-1",
            model_id="model-1",
            user_id=7,
            run_type="FIRST_TIME",
            status="RUNNING",
            environment={"python": "3.12"},
            hyperparameters={"lr": 0.01},
            code_artifact_id=None,
            weights_artifact_id=None,
            logs_artifact_id=None,
            code_snapshot_uri=None,
            code_snapshot_sha256=None,
            weights_uri=None,
            logs_uri=None,
            worker_id=None,
            error_summary=None,
            finished_at=None,
        )
        query = self.db.query.return_value.filter.return_value
        query.first.return_value = self.run
        query.with_for_update.return_value.first.return_value = self.run

    @patch("logic.mlflow.get_verified_artifact", side_effect=OSError("storage read failed"))
    def test_code_storage_failure_finishes_run_and_persists_incomplete_snapshot(self, verify):
        result = finish_run(
            self.db,
            3,
            self.user,
            {"status": "SUCCEEDED", "code_artifact_id": "code-1"},
        )

        snapshot = self.db.add.call_args.args[0]
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.logging_state, "DEGRADED")
        self.assertEqual(snapshot.state, "INCOMPLETE")
        self.assertIsNone(snapshot.code_artifact_id)
        self.assertTrue(any("Code snapshot artifact was unavailable" in reason for reason in snapshot.completeness_reasons))
        self.db.commit.assert_called_once()
        verify.assert_called_once()

    @patch("logic.mlflow.get_verified_artifact")
    def test_optional_artifact_storage_failure_makes_snapshot_incomplete(self, verify):
        code = SimpleNamespace(id="code-1", sha256="a" * 64)
        verify.side_effect = [
            (code, Path("/private/code.zip")),
            OSError("storage read failed"),
        ]

        result = finish_run(
            self.db,
            3,
            self.user,
            {
                "status": "SUCCEEDED",
                "code_artifact_id": "code-1",
                "weights_artifact_id": "weights-1",
            },
        )

        snapshot = self.db.add.call_args.args[0]
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.logging_state, "DEGRADED")
        self.assertEqual(snapshot.state, "INCOMPLETE")
        self.assertEqual(snapshot.code_artifact_id, "code-1")
        self.assertIsNone(snapshot.weights_artifact_id)
        self.assertTrue(any("Weights artifact was unavailable" in reason for reason in snapshot.completeness_reasons))
        self.db.commit.assert_called_once()
        self.assertEqual(verify.call_count, 2)
