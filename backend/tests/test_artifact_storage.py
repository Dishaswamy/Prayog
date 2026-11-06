import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from blake3 import blake3

from logic import artifact_storage
from logic.errors import BadRequestError


class ArtifactStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "artifacts"
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



    def test_content_addressed_local_object_rejects_mismatched_bytes(self):
        expected = b"original immutable payload"
        key = blake3(expected).hexdigest()
        source = self.root / "source.tmp"
        source.write_bytes(expected)

        stored = artifact_storage.persist_artifact(source, key)
        self.assertEqual(stored.read_bytes(), expected)

        source.write_bytes(b"different payload")
        with self.assertRaisesRegex(BadRequestError, "Content-addressed artifact integrity"):
            artifact_storage.persist_artifact(source, key)

        self.assertEqual(stored.read_bytes(), expected)

    def test_local_storage_rejects_symlink_escape_from_artifact_root(self):
        key = "12345678-1234-1234-1234-123456789abc"
        outside = Path(self.temporary.name) / "outside.bin"
        outside.write_bytes(b"outside")
        (self.root / key).symlink_to(outside)

        with self.assertRaisesRegex(BadRequestError, "Invalid artifact storage key"):
            artifact_storage.resolve_artifact(key)


    @patch("boto3.client")
    def test_s3_cache_discards_corrupt_content_addressed_copy_and_redownloads(self, make_client):
        payload = b"authoritative remote content"
        key = blake3(payload).hexdigest()
        cache = self.root / ".s3-cache"
        cache.mkdir()
        cached = cache / key
        cached.write_bytes(b"stale corrupted bytes")
        client = make_client.return_value

        def download(bucket, object_key, destination):
            Path(destination).write_bytes(payload)

        client.download_file.side_effect = download
        with patch.dict(os.environ, {
            "PRAYOG_ARTIFACT_STORAGE": "s3",
            "PRAYOG_S3_BUCKET": "private-prayog-test",
            "PRAYOG_S3_PREFIX": "ml-flow",
        }, clear=False):
            resolved = artifact_storage.resolve_artifact(key)

        self.assertEqual(resolved.read_bytes(), payload)
        self.assertEqual(resolved, cached)
        client.download_file.assert_called_once_with(
            "private-prayog-test", "ml-flow/" + key, ANY
        )

    @patch("boto3.client")
    def test_s3_rejects_existing_content_object_with_conflicting_digest_metadata(self, make_client):
        payload = b"expected content"
        key = blake3(payload).hexdigest()
        source = self.root / "upload.tmp"
        source.write_bytes(payload)
        client = make_client.return_value
        client.head_object.return_value = {
            "ContentLength": len(payload),
            "Metadata": {"blake3": "0" * 64},
        }

        with patch.dict(os.environ, {
            "PRAYOG_ARTIFACT_STORAGE": "s3",
            "PRAYOG_S3_BUCKET": "private-prayog-test",
            "PRAYOG_S3_PREFIX": "ml-flow",
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, "metadata conflicts"):
                artifact_storage.persist_artifact(source, key)

        client.download_file.assert_not_called()
        client.upload_file.assert_not_called()

if __name__ == "__main__":
    unittest.main()
