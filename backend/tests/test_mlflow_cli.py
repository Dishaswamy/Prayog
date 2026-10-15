import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from mlflow_cli import execute, parser


class MLFlowCliTests(unittest.TestCase):
    def test_start_run_preserves_metadata_and_adds_runtime_environment(self):
        args = parser().parse_args(["start-run", "--run-id", "8"])
        current_response = MagicMock()
        current_response.json.return_value = {
            "environment": {"requirements": ["numpy==2.0.0"], "python_version": "custom"}
        }
        start_response = MagicMock()
        start_response.json.return_value = {"id": 8, "status": "RUNNING"}
        client = MagicMock()
        client.get.return_value = current_response
        client.post.return_value = start_response

        execute(args, client)

        environment = client.post.call_args.kwargs["json"]["environment"]
        self.assertEqual(environment["requirements"], ["numpy==2.0.0"])
        self.assertEqual(environment["runtime_metadata_source"], "prayog-mlflow-cli")
        self.assertTrue(environment["python_version"])
        self.assertEqual(environment["os_name"], os.name)

    def test_start_run_can_queue_code_and_input_weights_for_worker(self):
        args = parser().parse_args([
            "start-run", "--run-id", "8",
            "--code-artifact-id", "code-1",
            "--weights-artifact-id", "weights-1",
        ])
        current_response = MagicMock()
        current_response.json.return_value = {"environment": {"python": "3.12"}}
        client = MagicMock()
        client.get.return_value = current_response
        client.post.return_value = MagicMock()

        execute(args, client)

        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["code_artifact_id"], "code-1")
        self.assertEqual(payload["weights_artifact_id"], "weights-1")

    def test_successful_finish_can_report_degraded_snapshot_logging(self):
        args = parser().parse_args([
            "finish-run", "--run-id", "8", "--experiment-id", "2", "--status", "SUCCEEDED"
        ])
        response = MagicMock()
        response.json.return_value = {"id": 8, "status": "SUCCEEDED", "logging_state": "DEGRADED"}
        client = MagicMock()
        client.post.return_value = response

        result = execute(args, client)

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["logging_state"], "DEGRADED")
        self.assertNotIn("code_artifact_id", client.post.call_args.kwargs["json"])

    def test_download_artifact_writes_to_requested_path(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "restored-code.zip"
            args = parser().parse_args([
                "download-artifact", "--artifact-id", "artifact-1", "--output", str(output)
            ])
            client = MagicMock()
            response = client.stream.return_value.__enter__.return_value
            response.iter_bytes.return_value = [b"part-", b"two"]
            result = execute(args, client)

            self.assertEqual(output.read_bytes(), b"part-two")
            self.assertEqual(result["size_bytes"], 8)
            client.stream.assert_called_once_with("GET", "/mlflow/artifacts/artifact-1/content")

    def test_finish_uploads_code_then_sends_server_artifact_id(self):
        with tempfile.TemporaryDirectory() as directory:
            code_file = Path(directory) / "code.zip"
            code_file.write_bytes(b"code archive")
            args = parser().parse_args([
                "finish-run", "--run-id", "8", "--experiment-id", "2",
                "--status", "SUCCEEDED", "--code-file", str(code_file),
            ])
            upload_response = MagicMock()
            upload_response.json.return_value = {"id": "server-artifact-id"}
            finish_response = MagicMock()
            finish_response.json.return_value = {"id": 8, "status": "SUCCEEDED"}
            client = MagicMock()
            client.post.side_effect = [upload_response, finish_response]

            result = execute(args, client)

            self.assertEqual(result["status"], "SUCCEEDED")
            upload_call, finish_call = client.post.call_args_list
            self.assertEqual(upload_call.args[0], "/mlflow/experiments/2/artifacts")
            self.assertEqual(upload_call.kwargs["params"], {"kind": "CODE"})
            self.assertEqual(finish_call.kwargs["json"]["code_artifact_id"], "server-artifact-id")


    def _sync_client(self):
        run = {
            "id": 8, "experiment_id": 2, "project_id": "project-1", "model_id": "model-1",
            "run_type": "REBUILD", "source_snapshot_id": "snapshot-1", "status": "SUCCEEDED",
            "logging_state": "COMPLETE", "logging_warnings": [], "error_summary": None,
            "created_at": "2026-10-15T04:00:00", "started_at": "2026-10-15T04:01:00",
            "finished_at": "2026-10-15T04:02:00", "weights_artifact_id": "weights-1",
            "logs_artifact_id": "logs-1",
        }
        weights = b"trained weights"
        logs = b"run completed"
        artifacts = [
            {"id": "weights-1", "kind": "WEIGHTS", "sha256": hashlib.sha256(weights).hexdigest(), "size_bytes": len(weights)},
            {"id": "logs-1", "kind": "LOG", "sha256": hashlib.sha256(logs).hexdigest(), "size_bytes": len(logs)},
        ]
        run_response = MagicMock()
        run_response.json.return_value = run
        artifacts_response = MagicMock()
        artifacts_response.json.return_value = artifacts
        client = MagicMock()
        client.get.side_effect = [run_response, artifacts_response]
        stream_responses = []
        for payload in (weights, logs):
            response = MagicMock()
            response.iter_bytes.return_value = [payload[:5], payload[5:]]
            context = MagicMock()
            context.__enter__.return_value = response
            stream_responses.append(context)
        client.stream.side_effect = stream_responses
        return client, weights, logs, artifacts_response

    def test_sync_results_writes_verified_artifacts_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            original_cwd = os.getcwd()
            try:
                os.chdir(directory)
                args = parser().parse_args(["sync-results", "--run-id", "8"])
                client, weights, logs, _ = self._sync_client()

                result = execute(args, client)

                result_dir = Path("results") / "8"
                self.assertEqual((result_dir / "weights.bin").read_bytes(), weights)
                self.assertEqual((result_dir / "logs.log").read_bytes(), logs)
                metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(metadata["run_id"], 8)
                self.assertEqual(metadata["status"], "SUCCEEDED")
                self.assertEqual([item["kind"] for item in metadata["artifacts"]], ["WEIGHTS", "LOG"])
                self.assertNotIn("hyperparameters", metadata)
                self.assertEqual(result["directory"], "results/8")
                self.assertTrue(result["metadata_changed"])
            finally:
                os.chdir(original_cwd)

    def test_repeat_sync_preserves_identical_result_files(self):
        with tempfile.TemporaryDirectory() as directory:
            original_cwd = os.getcwd()
            try:
                os.chdir(directory)
                args = parser().parse_args(["sync-results", "--run-id", "8"])
                first_client, _, _, _ = self._sync_client()
                execute(args, first_client)
                result_dir = Path("results") / "8"
                paths = [result_dir / "weights.bin", result_dir / "logs.log", result_dir / "metadata.json"]
                mtimes = [path.stat().st_mtime_ns for path in paths]

                second_client, _, _, _ = self._sync_client()
                result = execute(args, second_client)

                self.assertEqual([path.stat().st_mtime_ns for path in paths], mtimes)
                self.assertFalse(result["metadata_changed"])
                self.assertTrue(all(item["unchanged"] for item in result["artifacts"]))
            finally:
                os.chdir(original_cwd)

    def test_sync_results_rejects_artifact_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            original_cwd = os.getcwd()
            try:
                os.chdir(directory)
                client, _, _, artifact_response = self._sync_client()
                artifact_response.json.return_value[0]["sha256"] = "0" * 64
                args = parser().parse_args(["sync-results", "--run-id", "8"])

                with self.assertRaisesRegex(ValueError, "checksum"):
                    execute(args, client)

                self.assertFalse((Path("results") / "8" / "weights.bin").exists())
            finally:
                os.chdir(original_cwd)


    def test_sync_results_rejects_artifact_kind_mismatch_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            original_cwd = os.getcwd()
            try:
                os.chdir(directory)
                client, _, _, artifact_response = self._sync_client()
                artifact_response.json.return_value[0]["kind"] = "LOG"
                args = parser().parse_args(["sync-results", "--run-id", "8"])

                with self.assertRaisesRegex(ValueError, "unexpected kind"):
                    execute(args, client)

                self.assertFalse(Path("results").exists())
                client.stream.assert_not_called()
            finally:
                os.chdir(original_cwd)

    def test_sync_results_rejects_invalid_artifact_metadata_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            original_cwd = os.getcwd()
            try:
                os.chdir(directory)
                client, _, _, artifact_response = self._sync_client()
                artifact_response.json.return_value[0]["sha256"] = "not-a-digest"
                args = parser().parse_args(["sync-results", "--run-id", "8"])

                with self.assertRaisesRegex(ValueError, "invalid SHA-256"):
                    execute(args, client)

                self.assertFalse(Path("results").exists())
                client.stream.assert_not_called()
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
