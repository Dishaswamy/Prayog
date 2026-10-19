import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from logic.artifacts import get_verified_artifact, store_artifact
from logic.errors import BadRequestError


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
            with patch("logic.artifacts.ARTIFACT_ROOT", Path(directory).resolve()), patch("logic.artifact_storage.ARTIFACT_ROOT", Path(directory).resolve()):
                db = MagicMock()
                user = SimpleNamespace(id=9)
                experiment = SimpleNamespace(id=4, project_id="project-1", model_id="model-1")
                artifact = await store_artifact(
                    db, user, experiment, "CODE", FakeRequest([payload[:8], payload[8:]])
                )

                self.assertEqual(artifact.sha256, hashlib.sha256(payload).hexdigest())
                self.assertEqual(artifact.size_bytes, len(payload))
                stored = Path(directory).resolve() / artifact.storage_key
                self.assertEqual(stored.read_bytes(), payload)

                db.query.return_value.filter.return_value.first.return_value = artifact
                verified, path = get_verified_artifact(db, artifact.id, 9, 4, "CODE")
                self.assertIs(verified, artifact)
                self.assertEqual(path.read_bytes(), payload)

                stored.write_bytes(b"tampered")
                with self.assertRaises(BadRequestError):
                    get_verified_artifact(db, artifact.id, 9, 4, "CODE")

    async def test_empty_upload_is_rejected_and_not_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("logic.artifacts.ARTIFACT_ROOT", Path(directory).resolve()):
                db = MagicMock()
                with self.assertRaises(BadRequestError):
                    await store_artifact(
                        db, SimpleNamespace(id=9), SimpleNamespace(id=4), "CODE", FakeRequest([])
                    )
                db.add.assert_not_called()
                db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
