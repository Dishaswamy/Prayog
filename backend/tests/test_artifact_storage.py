import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from logic import artifact_storage


class ArtifactStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "artifacts"
        self.root.mkdir(mode=0o700)
        self.root_patch = patch.object(artifact_storage, "ARTIFACT_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.env_patch = patch.dict(os.environ, {"PRAYOG_ARTIFACT_STORAGE": "local"}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_local_storage_persists_and_resolves_private_file(self):
        source = self.root / "upload.tmp"
        source.write_bytes(b"local-artifact")

        path = artifact_storage.persist_artifact(source, "12345678-1234-1234-1234-123456789abc")

        self.assertEqual(path.read_bytes(), b"local-artifact")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            artifact_storage.resolve_artifact("12345678-1234-1234-1234-123456789abc"),
            path,
        )

    def test_rejects_unsafe_storage_key(self):
        with self.assertRaisesRegex(Exception, "Invalid artifact storage key"):
            artifact_storage.resolve_artifact("../private")

    @patch("boto3.client")
    def test_s3_mode_uploads_and_downloads_to_private_cache(self, make_client):
        client = make_client.return_value
        payload = b"s3-compatible-artifact"
        source = self.root / "upload.tmp"
        source.write_bytes(payload)

        with patch.dict(os.environ, {
            "PRAYOG_ARTIFACT_STORAGE": "s3",
            "PRAYOG_S3_BUCKET": "private-prayog-test",
            "PRAYOG_S3_PREFIX": "ml-flow",
            "PRAYOG_S3_REGION": "ap-south-1",
            "PRAYOG_S3_SERVER_SIDE_ENCRYPTION": "AES256",
        }, clear=False):
            artifact_storage.persist_artifact(source, "12345678-1234-1234-1234-123456789abc")
            client.upload_file.assert_called_once_with(
                str(source),
                "private-prayog-test",
                "ml-flow/12345678-1234-1234-1234-123456789abc",
                ExtraArgs={"ServerSideEncryption": "AES256"},
            )

            def download(bucket, key, destination):
                self.assertEqual(bucket, "private-prayog-test")
                self.assertEqual(key, "ml-flow/12345678-1234-1234-1234-123456789abc")
                Path(destination).write_bytes(payload)

            client.download_file.side_effect = download
            resolved = artifact_storage.resolve_artifact("12345678-1234-1234-1234-123456789abc")
            self.assertEqual(resolved.read_bytes(), payload)
            self.assertEqual(resolved.stat().st_mode & 0o777, 0o600)
            artifact_storage.delete_artifact("12345678-1234-1234-1234-123456789abc")
            client.delete_object.assert_called_once_with(
                Bucket="private-prayog-test",
                Key="ml-flow/12345678-1234-1234-1234-123456789abc",
            )
            self.assertFalse(resolved.exists())

    @patch("boto3.client")
    def test_s3_cache_cannot_reuse_a_local_artifact_with_the_same_key(self, make_client):
        key = "12345678-1234-1234-1234-123456789abc"
        source = self.root / "local-source.tmp"
        source.write_bytes(b"local")
        local_path = artifact_storage.persist_artifact(source, key)
        self.assertEqual(local_path.read_bytes(), b"local")

        client = make_client.return_value
        with patch.dict(os.environ, {
            "PRAYOG_ARTIFACT_STORAGE": "s3",
            "PRAYOG_S3_BUCKET": "private-prayog-test",
            "PRAYOG_S3_PREFIX": "ml-flow",
        }, clear=False):
            def download(bucket, object_key, destination):
                Path(destination).write_bytes(b"remote")

            client.download_file.side_effect = download
            remote_path = artifact_storage.resolve_artifact(key)
            self.assertNotEqual(remote_path, local_path)
            self.assertEqual(remote_path.read_bytes(), b"remote")
            client.download_file.assert_called_once()

    def test_s3_mode_requires_bucket_configuration(self):
        with patch.dict(os.environ, {
            "PRAYOG_ARTIFACT_STORAGE": "s3",
            "PRAYOG_S3_BUCKET": "",
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, "PRAYOG_S3_BUCKET is required"):
                artifact_storage.resolve_artifact("12345678-1234-1234-1234-123456789abc")


if __name__ == "__main__":
    unittest.main()
