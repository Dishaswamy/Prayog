import hashlib
import io
import os
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

os.environ.setdefault("POSTGRES_USER", "prayog")
os.environ.setdefault("POSTGRES_PASSWORD", "prayog-test-password")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "prayog_test")
os.environ.setdefault("JWT_SECRET_KEY", "ci-only-secret-not-for-production")

from fastapi.testclient import TestClient
from database import SessionLocal
from main import app
from mlflow_models import Artifact, DiagnosticLink, ModelSnapshot, RebuildAttempt
from mlflow_worker import claim_next_job, execute_first_time_run

IMAGE = os.getenv("PRAYOG_WORKER_IMAGE", "prayog-ml-runtime:python3.12")


@unittest.skipUnless(os.getenv("PRAYOG_RUN_E2E") == "1", "PostgreSQL and Docker end-to-end test runs in CI")
class MLFlowEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        email = f"mlflow-e2e-{uuid4().hex}@iitk.ac.in"
        response = self.client.post(
            "/auth/register",
            json={"email": email, "password": "Test-password-2026!", "role": "STUDENT"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        response = self.client.post(
            "/auth/login",
            json={"email": email, "password": "Test-password-2026!"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.client.headers.update({"Authorization": f"Bearer {response.json()['access_token']}"})

    def create_run(self, script):
        response = self.client.post("/mlflow/projects", json={"name": "E2E project"})
        self.assertEqual(response.status_code, 201, response.text)
        project_id = response.json()["project_id"]

        response = self.client.post(
            f"/mlflow/projects/{project_id}/models", json={"name": "E2E model"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        model_id = response.json()["model_id"]

        response = self.client.post(
            "/mlflow/experiments",
            json={"project_id": project_id, "model_id": model_id, "name": "E2E experiment"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        experiment_id = response.json()["id"]

        response = self.client.post(
            f"/mlflow/experiments/{experiment_id}/runs",
            json={"hyperparameters": {"test": True}, "environment": {"python": "3.12"}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        run_id = response.json()["id"]

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr("main.py", script)
        response = self.client.post(
            f"/mlflow/experiments/{experiment_id}/artifacts",
            params={"kind": "CODE"},
            content=archive.getvalue(),
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        code_artifact_id = response.json()["id"]

        response = self.client.post(
            f"/mlflow/runs/{run_id}/start",
            json={
                "environment": {"entrypoint": "main.py", "python_version": "3.12"},
                "code_artifact_id": code_artifact_id,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "QUEUED")
        return experiment_id, run_id, code_artifact_id

    def run_one_queued_job(self, expected_run_id):
        worker_id = f"e2e-worker-{uuid4().hex}"
        db = SessionLocal()
        try:
            job = claim_next_job(db, worker_id)
            self.assertEqual(job, ("FIRST_TIME", expected_run_id))
            execute_first_time_run(db, expected_run_id, worker_id, 15, IMAGE)
        finally:
            db.close()

    def test_authenticated_api_queue_worker_artifacts_snapshot_and_first_time_failure(self):
        experiment_id, successful_run_id, code_artifact_id = self.create_run(
            "from pathlib import Path\n"
            "print('end-to-end-success', flush=True)\n"
            "Path('/output/weights.bin').write_bytes(b'e2e-weights')\n"
        )
        self.run_one_queued_job(successful_run_id)

        response = self.client.get(f"/mlflow/runs/{successful_run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        successful_run = response.json()
        self.assertEqual(successful_run["status"], "SUCCEEDED", successful_run)
        self.assertIsNotNone(successful_run["logs_artifact_id"])
        self.assertIsNotNone(successful_run["weights_artifact_id"])

        response = self.client.get(f"/mlflow/experiments/{experiment_id}/snapshots")
        self.assertEqual(response.status_code, 200, response.text)
        snapshots = response.json()
        self.assertEqual(len(snapshots), 1, snapshots)
        snapshot = snapshots[0]
        self.assertEqual(snapshot["state"], "RECORDED")
        self.assertEqual(snapshot["code_artifact_id"], code_artifact_id)
        self.assertEqual(snapshot["weights_artifact_id"], successful_run["weights_artifact_id"])

        response = self.client.get(f"/mlflow/snapshots/{snapshot['snapshot_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["snapshot_id"], snapshot["snapshot_id"])

        response = self.client.get(f"/mlflow/experiments/{experiment_id}/artifacts")
        self.assertEqual(response.status_code, 200, response.text)
        artifacts = response.json()
        self.assertTrue(any(item["id"] == code_artifact_id and item["kind"] == "CODE" for item in artifacts))
        self.assertTrue(any(item["id"] == successful_run["logs_artifact_id"] and item["kind"] == "LOG" for item in artifacts))
        self.assertTrue(any(item["id"] == successful_run["weights_artifact_id"] and item["kind"] == "WEIGHTS" for item in artifacts))

        rebuild_response = self.client.post(
            f"/mlflow/snapshots/{snapshot['snapshot_id']}/rebuilds",
            json={"environment": {"entrypoint": "main.py"}},
        )
        self.assertEqual(rebuild_response.status_code, 201, rebuild_response.text)
        rebuild_id = rebuild_response.json()["id"]

        blocked_link = self.client.post(
            f"/mlflow/rebuilds/{rebuild_id}/diagnostic-link",
            json={"ttl_seconds": 60},
        )
        self.assertEqual(blocked_link.status_code, 409, blocked_link.text)

        diagnostic_payload = b"rebuild failed: intentional e2e diagnostic"
        response = self.client.post(
            f"/mlflow/experiments/{experiment_id}/artifacts",
            params={"kind": "LOG"},
            content=diagnostic_payload,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        diagnostic_artifact_id = response.json()["id"]
        response = self.client.patch(
            f"/mlflow/rebuilds/{rebuild_id}",
            json={
                "state": "REBUILD_FAILED",
                "logs_artifact_id": diagnostic_artifact_id,
                "failure_summary": "intentional diagnostic-link test failure",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(
            f"/mlflow/rebuilds/{rebuild_id}/diagnostic-link",
            json={"ttl_seconds": 60},
        )
        self.assertEqual(response.status_code, 201, response.text)
        diagnostic_link = response.json()
        self.assertIn("/mlflow/diagnostic-artifacts/", diagnostic_link["path"])
        self.assertIsNotNone(diagnostic_link["expires_at"])

        response = self.client.get(diagnostic_link["path"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, diagnostic_payload)
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        response = self.client.get(diagnostic_link["path"])
        self.assertEqual(response.status_code, 404, response.text)

        expired_link_response = self.client.post(
            f"/mlflow/rebuilds/{rebuild_id}/diagnostic-link",
            json={"ttl_seconds": 60},
        )
        self.assertEqual(expired_link_response.status_code, 201, expired_link_response.text)
        expired_path = expired_link_response.json()["path"]
        expired_token = expired_path.rsplit("/", 1)[-1]
        db = SessionLocal()
        try:
            link_record = db.query(DiagnosticLink).filter(
                DiagnosticLink.token_hash == hashlib.sha256(expired_token.encode("utf-8")).hexdigest()
            ).first()
            self.assertIsNotNone(link_record)
            self.assertNotEqual(link_record.token_hash, expired_token)
            link_record.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            db.commit()
        finally:
            db.close()
        response = self.client.get(expired_path)
        self.assertEqual(response.status_code, 404, response.text)

        failed_experiment_id, failed_run_id, _ = self.create_run(
            "print('end-to-end-intentional-failure', flush=True)\n"
            "raise SystemExit(7)\n"
        )
        self.run_one_queued_job(failed_run_id)

        response = self.client.get(f"/mlflow/runs/{failed_run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        failed_run = response.json()
        self.assertEqual(failed_run["status"], "FAILED", failed_run)
        self.assertIn("exit code 7", failed_run["error_summary"])
        self.assertIsNotNone(failed_run["logs_artifact_id"])

        response = self.client.get(f"/mlflow/experiments/{failed_experiment_id}/snapshots")
        self.assertEqual(response.status_code, 200, response.text)
        failed_snapshots = response.json()
        self.assertEqual(len(failed_snapshots), 1, failed_snapshots)
        response = self.client.get(f"/mlflow/snapshots/{failed_snapshots[0]['snapshot_id']}/rebuilds")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [])

        db = SessionLocal()
        try:
            self.assertEqual(
                db.query(RebuildAttempt).filter(
                    RebuildAttempt.snapshot_id == failed_snapshots[0]["snapshot_id"]
                ).count(),
                0,
            )
            self.assertEqual(
                db.query(ModelSnapshot).filter(ModelSnapshot.run_id == successful_run_id).count(),
                1,
            )
            self.assertEqual(
                db.query(Artifact).filter(Artifact.id == successful_run["weights_artifact_id"]).count(),
                1,
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
