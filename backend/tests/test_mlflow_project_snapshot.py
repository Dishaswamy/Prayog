import hashlib
import json
import tempfile
import zipfile
import unittest
from pathlib import Path

from mlflow_cli import _extract_verified_project_archive, materialize_project_snapshot, snapshot_project


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeStream:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        for offset in range(0, len(self.payload), 65536):
            yield self.payload[offset:offset + 65536]


class FakeArtifactClient:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.post_count = 0

    def get(self, path):
        if not path.endswith("/artifacts"):
            raise AssertionError(f"Unexpected GET {path}")
        return FakeResponse(list(self.metadata.values()))

    def post(self, path, params, content, headers):
        payload = content.read()
        self.post_count += 1
        artifact_id = f"artifact-{self.post_count}"
        item = {
            "id": artifact_id,
            "kind": params["kind"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        self.objects[artifact_id] = payload
        self.metadata[artifact_id] = item
        return FakeResponse({"id": artifact_id})

    def stream(self, method, path):
        if method != "GET":
            raise AssertionError(f"Unexpected method {method}")
        artifact_id = path.split("/")[-2]
        return FakeStream(self.objects[artifact_id])


class ProjectSnapshotTests(unittest.TestCase):
    def _make_project(self, root):
        (root / "src").mkdir()
        (root / "data").mkdir()
        (root / ".git").mkdir()
        (root / "src" / "main.py").write_text("print('uncommitted edit')\n", encoding="utf-8")
        (root / "requirements.txt").write_text("numpy==2.0.0\n", encoding="utf-8")
        (root / "config.yaml").write_text("learning_rate: 0.01\n", encoding="utf-8")
        (root / "seed.txt").write_text("seed=42\n", encoding="utf-8")
        (root / "data" / "training.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        (root / ".env.example").write_text("PRAYOG_API_URL=http://localhost:8000\n", encoding="utf-8")
        (root / ".env").write_text("SECRET=do-not-upload\n", encoding="utf-8")
        (root / "id_rsa").write_text("PRIVATE KEY MATERIAL\n", encoding="utf-8")
        (root / ".ssh").mkdir()
        (root / ".ssh" / "config").write_text("private host configuration\n", encoding="utf-8")
        (root / ".git-credentials").write_text("https://user:token@example.com\n", encoding="utf-8")
        (root / "credentials.toml").write_text("token = \"private\"\n", encoding="utf-8")
        (root / "terraform.tfstate").write_text("state with provider secrets\n", encoding="utf-8")
        (root / ".terraform").mkdir()
        (root / ".terraform" / "state.json").write_text("provider state\n", encoding="utf-8")
        (root / ".git" / "config").write_text("private git metadata\n", encoding="utf-8")

    def test_snapshot_uploads_missing_chunks_and_materializes_verified_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            self._make_project(root)
            client = FakeArtifactClient()

            first = snapshot_project(client, 2, root, chunk_size_mib=1)
            upload_count = client.post_count
            second = snapshot_project(client, 2, root, chunk_size_mib=1)

            self.assertGreater(first["file_count"], 0)
            self.assertEqual(first["uploaded_chunks"], 1)
            self.assertEqual(second["uploaded_chunks"], 0)
            self.assertEqual(second["reused_chunks"], 1)
            self.assertFalse(second["manifest_uploaded"])
            self.assertEqual(client.post_count, upload_count)
            self.assertEqual(first["manifest_artifact_id"], second["manifest_artifact_id"])

            output = Path(directory) / "materialized"
            result = materialize_project_snapshot(
                client, 2, first["manifest_artifact_id"], output, data_dir=root
            )

            self.assertTrue(result["verified"])
            self.assertEqual(
                (output / "src" / "main.py").read_text(encoding="utf-8"),
                "print('uncommitted edit')\n",
            )
            self.assertTrue((output / "requirements.txt").is_file())
            self.assertTrue((output / "config.yaml").is_file())
            self.assertTrue((output / "seed.txt").is_file())
            self.assertFalse((output / "data" / "training.csv").exists())
            self.assertTrue((output / ".env.example").is_file())
            self.assertFalse((output / ".env").exists())
            self.assertFalse((output / "id_rsa").exists())
            self.assertFalse((output / ".ssh").exists())
            self.assertFalse((output / ".git-credentials").exists())
            self.assertFalse((output / "credentials.toml").exists())
            self.assertFalse((output / "terraform.tfstate").exists())
            self.assertFalse((output / ".terraform").exists())
            self.assertFalse((output / ".git").exists())
            internal_manifest = json.loads(
                (output / "snapshot-manifest.json").read_text(encoding="utf-8")
            )
            data_entry = next(item for item in internal_manifest["files"] if item["path"] == "data/training.csv")
            self.assertEqual(data_entry["role"], "data")
            self.assertFalse(data_entry["included_in_archive"])
            self.assertEqual(data_entry["sha256"], hashlib.sha256(b"x,y\n1,2\n").hexdigest())
            self.assertTrue(result["data_fingerprints"])

    def test_materialization_rejects_a_tampered_chunk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            self._make_project(root)
            client = FakeArtifactClient()
            result = snapshot_project(client, 2, root, chunk_size_mib=1)
            manifest = json.loads(
                client.objects[result["manifest_artifact_id"]].decode("utf-8")
            )
            chunk_id = manifest["chunks"][0]["artifact_id"]
            client.objects[chunk_id] = b"tampered chunk content"

            output = Path(directory) / "materialized"
            with self.assertRaisesRegex(ValueError, "(size|checksum)"):
                materialize_project_snapshot(client, 2, result["manifest_artifact_id"], output, data_dir=root)
            self.assertFalse(output.exists())


    def test_materialization_rejects_changed_dataset_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            self._make_project(root)
            client = FakeArtifactClient()
            result = snapshot_project(client, 2, root, chunk_size_mib=1)
            (root / "data" / "training.csv").write_text("x,y\\n9,9\\n", encoding="utf-8")
            output = Path(directory) / "materialized"

            with self.assertRaisesRegex(ValueError, "Dataset fingerprint verification failed"):
                materialize_project_snapshot(
                    client, 2, result["manifest_artifact_id"], output, data_dir=root
                )
            self.assertFalse(output.exists())

    def test_archive_materialization_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "malicious.zip"
            staging = root / "staging"
            staging.mkdir()
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "do not extract")
            with self.assertRaisesRegex(ValueError, "unsafe file path"):
                _extract_verified_project_archive(archive_path, staging, [])
            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
