import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret")
os.environ.setdefault("POSTGRES_USER", "prayog")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "prayog_test")

from fastapi.testclient import TestClient

from database import get_db
from logic.auth.dependencies import get_current_user
from main import app
from logic.mlflow.artifacts import get_verified_artifact, store_artifact
from logic.common.errors import BadRequestError


class FakeRequest:
    def __init__(self, chunks):
        self.chunks = chunks

    async def stream(self):
        for chunk in self.chunks:
            yield chunk


class ArtifactStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_records_server_computed_digest_and_download_verifies_it(self):
        payload = b"private model source archive"
        with tempfile.TemporaryDirectory() as directory:
            with patch("logic.mlflow.artifacts.ARTIFACT_ROOT", Path(directory).resolve()), patch("logic.common.artifact_storage.ARTIFACT_ROOT", Path(directory).resolve()):
                db = MagicMock()
                user = SimpleNamespace(id=9)
                experiment = SimpleNamespace(id=4, project_id="project-1", model_id="model-1")
                artifact = await store_artifact(
                    db, user, experiment, "CODE", FakeRequest([payload[:8], payload[8:]])
                )

                self.assertEqual(artifact.sha256, hashlib.sha256(payload).hexdigest())
                self.assertEqual(artifact.size_bytes, len(payload))
                stored = Path(directory) / artifact.storage_key
                self.assertEqual(stored.read_bytes(), payload)

                db.query.return_value.filter.return_value.first.return_value = artifact
                verified, path = get_verified_artifact(db, artifact.id, 9, 4, "CODE")
                self.assertIs(verified, artifact)
                self.assertEqual(path.read_bytes(), payload)

                stored.write_bytes(b"tampered")
                with self.assertRaises(BadRequestError):
                    get_verified_artifact(db, artifact.id, 9, 4, "CODE")

    async def test_nothing_is_awaited_while_the_experiment_is_locked(self):
        # The lock is taken by a blocking call on the event loop. An await while holding it lets a second
        # upload to the same experiment block the loop on that lock, and the holder can then never resume:
        # the worker freezes for good. This froze the Render deployment twice on 12 Nov 2026.
        import anyio

        order = []
        real_run_sync = anyio.to_thread.run_sync

        async def run_sync(fn, *args, **kwargs):
            order.append(getattr(fn, "__name__", "thread"))
            return await real_run_sync(fn, *args, **kwargs)

        db = MagicMock()
        db.query.return_value.filter.return_value.with_for_update.side_effect = (
            lambda *a, **k: order.append("lock") or MagicMock())
        db.commit.side_effect = lambda: order.append("commit")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch("logic.mlflow.artifacts.ARTIFACT_ROOT", root), \
                    patch("logic.common.artifact_storage.ARTIFACT_ROOT", root), \
                    patch.object(anyio.to_thread, "run_sync", run_sync):
                await store_artifact(db, SimpleNamespace(id=9), SimpleNamespace(id=4, project_id="p", model_id="m"),
                                     "WEIGHTS", FakeRequest([b"weights"]))

        self.assertLess(order.index("persist_artifact"), order.index("lock"))
        self.assertEqual(order[order.index("lock"):], ["lock", "commit"])

    async def test_empty_upload_is_rejected_and_not_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("logic.mlflow.artifacts.ARTIFACT_ROOT", Path(directory)):
                db = MagicMock()
                with self.assertRaises(BadRequestError):
                    await store_artifact(
                        db, SimpleNamespace(id=9), SimpleNamespace(id=4), "CODE", FakeRequest([])
                    )
                db.add.assert_not_called()
                db.commit.assert_not_called()



class ArtifactDownloadRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=9, role="STUDENT")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_user_cannot_download_another_users_artifact(self):
        self.db.query.return_value.filter.return_value.first.return_value = None
        with patch("routers.mlflow.experiments.get_verified_artifact") as verify:
            response = self.client.get("/mlflow/artifacts/foreign-artifact/content")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Artifact not found"})
        verify.assert_not_called()

    def test_owned_artifact_download_is_verified_and_private(self):
        import tempfile

        artifact = SimpleNamespace(
            id="artifact-1", user_id=9, experiment_id=4, kind="CODE"
        )
        self.db.query.return_value.filter.return_value.first.return_value = artifact
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "verified.bin"
            path.write_bytes(b"verified-artifact")
            with patch("routers.mlflow.experiments.get_verified_artifact", return_value=(artifact, path)) as verify:
                response = self.client.get("/mlflow/artifacts/artifact-1/content")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"verified-artifact")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        verify.assert_called_once_with(self.db, "artifact-1", 9, 4, "CODE")

    def test_user_cannot_upload_artifact_to_another_users_experiment(self):
        from logic.common.errors import NotFoundError

        # active_user checks the student's access window; give it a row with no end date.
        self.db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(end_date=None)
        with patch("routers.mlflow.experiments._owned_experiment", side_effect=NotFoundError("Experiment not found")) as owned, patch(
            "routers.mlflow.experiments.store_artifact"
        ) as store:
            response = self.client.post(
                "/mlflow/experiments/404/artifacts",
                params={"kind": "CODE"},
                content=b"private source archive",
                headers={"Content-Type": "application/octet-stream"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Experiment not found"})
        owned.assert_called_once_with(self.db, 404, self.user)
        store.assert_not_called()

    def test_user_cannot_list_artifacts_from_another_users_experiment(self):
        from logic.common.errors import NotFoundError

        with patch(
            "routers.mlflow.experiments._owned_experiment",
            side_effect=NotFoundError("Experiment not found"),
        ) as owned:
            response = self.client.get("/mlflow/experiments/404/artifacts")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Experiment not found"})
        owned.assert_called_once_with(self.db, 404, self.user)
        self.db.query.assert_not_called()

    def test_professor_with_inventory_access_cannot_download_owner_artifact_bytes(self):
        self.user = SimpleNamespace(id=20, role="PROFESSOR")
        self.db.query.return_value.filter.return_value.first.return_value = None

        with patch("routers.mlflow.experiments.get_verified_artifact") as verify:
            response = self.client.get("/mlflow/artifacts/student-artifact/content")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Artifact not found"})
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
