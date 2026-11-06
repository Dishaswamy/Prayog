import os
import unittest

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret")
os.environ.setdefault("POSTGRES_USER", "prayog")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "prayog_test")

from main import app


class MLFlowRouteRegistrationTests(unittest.TestCase):
    def test_mlflow_routes_are_registered_in_openapi(self):
        schema = app.openapi()
        paths = schema["paths"]
        expected = [
            "/mlflow/projects",
            "/mlflow/projects/{project_id}/models",
            "/mlflow/experiments",
            "/mlflow/experiments/{experiment_id}/artifacts",
            "/mlflow/experiments/{experiment_id}/runs",
            "/mlflow/runs/{run_id}/start",
            "/mlflow/runs/{run_id}/finish",
            "/mlflow/snapshots/{snapshot_id}/rebuilds",
            "/mlflow/rebuilds/{rebuild_id}",
            "/mlflow/artifacts/{artifact_id}/content",
        ]
        for path in expected:
            with self.subTest(path=path):
                self.assertIn(path, paths)


if __name__ == "__main__":
    unittest.main()
