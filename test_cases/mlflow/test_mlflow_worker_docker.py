import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from mlflow_worker import (
    build_docker_command,
    prepare_input_directory,
    run_container,
)

IMAGE = os.getenv("PRAYOG_WORKER_IMAGE", "prayog-ml-runtime:python3.12")


@unittest.skipUnless(os.getenv("PRAYOG_RUN_DOCKER_SMOKE") == "1", "Docker smoke test runs in CI")
class MLFlowWorkerDockerSmokeTests(unittest.TestCase):
    def run_script(self, script: str, timeout: int = 10):
        temporary = tempfile.TemporaryDirectory(prefix="prayog-docker-smoke-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "workspace"
        workspace.mkdir()
        entrypoint = workspace / "main.py"
        entrypoint.write_text(script, encoding="utf-8")
        entrypoint.chmod(0o444)
        workspace.chmod(0o555)
        input_dir = root / "input"
        prepare_input_directory(input_dir)
        name = f"prayog-smoke-{uuid4().hex[:12]}"
        logs = root / "container.log"
        weights = root / "weights.bin"
        completion_token = uuid4().hex
        command = build_docker_command(
            IMAGE, name, workspace, input_dir, Path("main.py"), 1,
            "smoke-snapshot", {"smoke": True}, False, completion_token,
        )
        result = run_container(command, name, logs, timeout, weights, completion_token)
        return result, logs, weights

    def test_container_executes_entrypoint_and_returns_output_artifact(self):
        result, logs, weights = self.run_script(
            "from pathlib import Path\n"
            "print('smoke-ok', flush=True)\n"
            "Path('/output/weights.bin').write_bytes(b'test-weights')\n"
        )

        self.assertEqual(result, (0, False, True), logs.read_text(encoding="utf-8"))
        self.assertIn("smoke-ok", logs.read_text(encoding="utf-8"))
        self.assertEqual(weights.read_bytes(), b"test-weights")

    def test_container_failure_returns_real_nonzero_exit_code(self):
        result, logs, _ = self.run_script(
            "print('intentional-failure', flush=True)\n"
            "raise SystemExit(7)\n"
        )

        self.assertEqual(result, (7, False, False))
        self.assertIn("intentional-failure", logs.read_text(encoding="utf-8"))

    def test_container_timeout_kills_execution(self):
        result, logs, _ = self.run_script(
            "import time\n"
            "print('started', flush=True)\n"
            "time.sleep(15)\n",
            timeout=1,
        )

        self.assertEqual(result[1], True)
        self.assertIn("execution exceeded the time limit", logs.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
