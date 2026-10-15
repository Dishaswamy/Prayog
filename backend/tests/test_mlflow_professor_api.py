"""HTTP-level authorization and read-only contract tests for Professor ML Flow."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from database import get_db
from logic.dependencies import get_current_user
from logic.errors import NotFoundError
from main import app


class ProfessorMLFlowAPITests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=10, role="PROFESSOR")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_non_professor_cannot_call_professor_endpoint(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=20, role="STUDENT"
        )

        response = self.client.get("/mlflow/professor/projects")

        self.assertEqual(response.status_code, 403)
        self.db.query.assert_not_called()

    def test_platform_admin_cannot_use_professor_access_as_artifact_access(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=1, role="PLATFORM_ADMIN"
        )

        response = self.client.get("/mlflow/professor/projects")

        self.assertEqual(response.status_code, 403)
        self.db.query.assert_not_called()

    @patch("routers.mlflow_professor.list_projects")
    def test_professor_can_list_projects_from_authorized_service(self, list_projects):
        list_projects.return_value = [
            SimpleNamespace(
                project_id="project-1",
                user_id=20,
                name="Research project",
                description=None,
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1),
            )
        ]

        response = self.client.get("/mlflow/professor/projects")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["project_id"] for item in response.json()], ["project-1"])
        list_projects.assert_called_once_with(self.db, self.user)

    @patch("routers.mlflow_professor.list_experiments")
    def test_experiment_tags_redact_credential_like_keys(self, list_experiments):
        now = datetime(2026, 1, 1)
        list_experiments.return_value = [
            SimpleNamespace(
                id=1, project_id="project-1", model_id="model-1", user_id=20,
                name="Experiment", description=None,
                tags={"api_key": "do-not-return", "dataset": "small"},
                created_at=now, updated_at=now,
            )
        ]

        response = self.client.get("/mlflow/professor/projects/project-1/experiments")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["tags"]["api_key"], "[REDACTED]")
        self.assertEqual(response.json()[0]["tags"]["dataset"], "small")

    @patch("routers.mlflow_professor.get_run")
    def test_unrelated_run_is_returned_as_not_found(self, get_run):
        get_run.side_effect = NotFoundError("Run not found")

        response = self.client.get("/mlflow/professor/runs/999")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Run not found"})

    @patch("routers.mlflow_professor.get_run")
    def test_run_response_does_not_expose_artifact_uris(self, get_run):
        now = datetime(2026, 1, 1)
        get_run.return_value = SimpleNamespace(
            id=1, experiment_id=2, project_id="project-1", model_id="model-1",
            user_id=20, run_type="FIRST_TIME", source_snapshot_id=None,
            status="SUCCEEDED", logging_state="COMPLETE", logging_warnings=[],
            hyperparameters={"learning_rate": 0.01, "api_key": "do-not-return"},
            environment={"python": "3.12", "nested": {"access_token": "do-not-return"}},
            code_snapshot_uri="private://code",
            code_snapshot_sha256="a" * 64, weights_uri="private://weights",
            logs_uri="private://logs", code_artifact_id="code-id",
            weights_artifact_id="weights-id", logs_artifact_id="logs-id",
            error_summary="worker failed with token=supersecret", created_at=now, started_at=now, finished_at=now,
            updated_at=now,
        )

        response = self.client.get("/mlflow/professor/runs/1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("code_snapshot_uri", payload)
        self.assertNotIn("weights_uri", payload)
        self.assertNotIn("logs_uri", payload)
        self.assertNotIn("private://", response.text)
        self.assertEqual(payload["hyperparameters"]["api_key"], "[REDACTED]")
        self.assertEqual(payload["environment"]["nested"]["access_token"], "[REDACTED]")
        self.assertEqual(payload["hyperparameters"]["learning_rate"], 0.01)
        self.assertEqual(payload["error_summary"], "worker failed with token=[REDACTED]")

    @patch("routers.mlflow_professor.get_snapshot")
    def test_snapshot_response_does_not_expose_artifact_uris(self, get_snapshot):
        get_snapshot.return_value = SimpleNamespace(
            id=1, snapshot_id="snapshot-1", experiment_id=2,
            project_id="project-1", model_id="model-1", run_id=3, user_id=20,
            state="RECORDED", code_uri="private://source-tree",
            code_sha256="b" * 64,
            dependencies={"private_key": "do-not-return", "python": "3.12"},
            hyperparameters={"secret_value": "do-not-return"},
            weights_uri="private://weights", code_artifact_id="code-id",
            weights_artifact_id="weights-id", logs_artifact_id="logs-id",
            created_at=datetime(2026, 1, 1),
        )

        response = self.client.get("/mlflow/professor/snapshots/snapshot-1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("code_uri", payload)
        self.assertNotIn("weights_uri", payload)
        self.assertNotIn("private://", response.text)
        self.assertEqual(payload["dependencies"]["private_key"], "[REDACTED]")
        self.assertEqual(payload["hyperparameters"]["secret_value"], "[REDACTED]")

    def test_professor_api_does_not_expose_mutating_run_route(self):
        response = self.client.post("/mlflow/professor/runs/1", json={"status": "REBUILD_SUCCESS"})

        self.assertEqual(response.status_code, 405)

    @patch("routers.mlflow_professor.get_rebuild")
    def test_rebuild_response_does_not_expose_log_uri(self, get_rebuild):
        now = datetime(2026, 1, 1)
        get_rebuild.return_value = SimpleNamespace(
            id=4, user_id=20, snapshot_id="snapshot-1", project_id="project-1",
            model_id="model-1", execution_run_id=3, state="REBUILD_FAILED",
            environment={"password": "do-not-return", "runtime": "python"},
            logs_uri="private://rebuild-logs",
            logs_artifact_id="logs-id", failure_summary="failed with api_key=supersecret",
            created_at=now, initiated_at=now, finished_at=now, updated_at=now,
        )

        response = self.client.get("/mlflow/professor/rebuilds/4")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("logs_uri", response.json())
        self.assertNotIn("private://", response.text)
        self.assertEqual(response.json()["environment"]["password"], "[REDACTED]")
        self.assertEqual(response.json()["environment"]["runtime"], "python")
        self.assertEqual(response.json()["failure_summary"], "failed with api_key=[REDACTED]")

    @patch("routers.mlflow_professor.list_artifacts")
    def test_artifact_inventory_response_does_not_expose_storage_key(self, list_artifacts):
        list_artifacts.return_value = [
            SimpleNamespace(
                id="artifact-1",
                user_id=20,
                experiment_id=3,
                project_id="project-1",
                model_id="model-1",
                kind="CODE",
                sha256="a" * 64,
                size_bytes=128,
                storage_key="private-storage-location",
                created_at=datetime(2026, 1, 1),
            )
        ]

        response = self.client.get("/mlflow/professor/experiments/3/artifacts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertNotIn("storage_key", response.json()[0])
        self.assertNotIn("download_url", response.json()[0])


if __name__ == "__main__":
    unittest.main()
