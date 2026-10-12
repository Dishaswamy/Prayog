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


if __name__ == "__main__":
    unittest.main()
