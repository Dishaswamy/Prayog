import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("POSTGRES_USER", "prayog")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "prayog_test")

from mlflow_worker import (
    build_docker_command,
    claim_next_first_time_run,
    extract_code_archive,
    prepare_input_directory,
    safe_output_file,
    validate_entrypoint,
)


class MLFlowWorkerTests(unittest.TestCase):
    def test_first_time_run_claim_is_committed_to_one_worker(self):
        from unittest.mock import MagicMock

        db = MagicMock()
        run = SimpleNamespace(id=41, status='QUEUED', started_at=None, worker_id=None, worker_claimed_at=None)
        query = db.query.return_value.filter.return_value
        query.order_by.return_value.with_for_update.return_value.first.return_value = run

        claimed = claim_next_first_time_run(db, "worker-1")

        self.assertEqual(claimed, 41)
        self.assertEqual(run.status, "RUNNING")
        self.assertIsNotNone(run.started_at)
        self.assertEqual(run.worker_id, "worker-1")
        self.assertIsNotNone(run.worker_claimed_at)
        db.commit.assert_called_once()
        db.rollback.assert_not_called()

    def test_extracts_archive_and_validates_relative_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "code.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("main.py", "print('ok')\n")
                output.writestr("lib/helper.py", "VALUE = 1\n")
            workspace = root / "workspace"

            extract_code_archive(archive, workspace)

            self.assertEqual(validate_entrypoint(workspace, "main.py"), Path("main.py"))
            self.assertEqual((workspace / "main.py").read_text(encoding="utf-8"), "print('ok')\n")
            self.assertFalse((workspace / "main.py").stat().st_mode & 0o222)

    def test_rejects_archive_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape.py", "print('unsafe')\n")

            with self.assertRaisesRegex(ValueError, "unsafe path"):
                extract_code_archive(archive, root / "workspace")

            self.assertFalse((root / "escape.py").exists())

    def test_rejects_entrypoint_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(ValueError, "safe relative path"):
                validate_entrypoint(workspace, "../outside.py")

    def test_container_command_enforces_isolation_and_no_gpu_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_docker_command(
                "prayog-ml-runtime:python3.12",
                "prayog-test-container",
                root / "workspace",
                root / "input",
                Path("main.py"),
                12,
                "snapshot-id",
                {"learning_rate": 0.01},
                True,
                "test-completion-token",
            )

            self.assertIn("--network", command)
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop", command)
            self.assertIn("ALL", command)
            self.assertIn("--pids-limit", command)
            self.assertIn("--memory", command)
            self.assertIn("--cpus", command)
            self.assertIn("--pull=never", command)
            self.assertIn("PRAYOG_COMPLETION_TOKEN=test-completion-token", command)
            self.assertTrue(any("type=tmpfs,destination=/output" in value for value in command))
            self.assertIn("PRAYOG_INPUT_WEIGHTS=/input/weights.bin", command)
            self.assertNotIn("--gpus", command)


    def test_input_weights_are_copied_before_mount_is_made_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-weights.bin"
            source.write_bytes(b"trained-weights")
            input_dir = root / "input"

            prepare_input_directory(input_dir, source)

            restored = input_dir / "weights.bin"
            self.assertEqual(restored.read_bytes(), b"trained-weights")
            self.assertFalse(input_dir.stat().st_mode & 0o222)
            self.assertFalse(restored.stat().st_mode & 0o222)


    def test_output_artifact_must_be_regular_file_not_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            weights = output / "weights.bin"
            weights.write_bytes(b"weights")
            self.assertEqual(safe_output_file(output, "weights.bin"), weights)

            link = output / "linked.bin"
            link.symlink_to(weights)
            self.assertIsNone(safe_output_file(output, "linked.bin"))


if __name__ == "__main__":
    unittest.main()
