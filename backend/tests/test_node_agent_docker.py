"""The same runs, in the real hardened container. Needs Docker and the runtime image (CI builds it from
backend/worker/Dockerfile): PRAYOG_RUN_DOCKER_SMOKE=1 PRAYOG_RUN_SCHEDULER_POSTGRES_TESTS=1."""
import json
import os
import subprocess
import unittest

from mlflow_models import RunCheckpoint
from node_agent.runners import DockerRunner
from tests.agent_case import AgentRunCase
from tests.gpu_case import requires_postgres

IMAGE = os.getenv("PRAYOG_WORKER_IMAGE", "prayog-ml-runtime:python3.12")

PROBE = '''\
import json, os, socket, pathlib

report = {"uid": os.getuid(), "env_secrets": [k for k in os.environ if any(s in k for s in ("POSTGRES", "JWT", "TOKEN", "CREDENTIAL"))]}

def attempt(name, action):
    try:
        action()
        report[name] = "allowed"
    except Exception as exc:
        report[name] = type(exc).__name__

attempt("write_code", lambda: pathlib.Path("/workspace/x").write_text("x"))
attempt("write_data", lambda: pathlib.Path("/data/x").write_text("x"))
attempt("write_root", lambda: pathlib.Path("/etc/x").write_text("x"))
attempt("network", lambda: socket.create_connection(("1.1.1.1", 53), timeout=3))
attempt("write_output", lambda: pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"], "ok.txt").write_text("x"))
attempt("write_tmp", lambda: pathlib.Path("/tmp/x").write_text("x"))
print("PROBE " + json.dumps(report))
'''


@unittest.skipUnless(os.getenv("PRAYOG_RUN_DOCKER_SMOKE") == "1", "Docker smoke tests run in CI")
@requires_postgres
class DockerAgentRunsTests(AgentRunCase):
    def docker_runner(self):
        return DockerRunner(IMAGE, memory="512m", cpus="1", pids=128, expose_gpus=False)   # this machine has no GPU

    def container_exists(self, allocation_id: int) -> bool:
        listed = subprocess.run(["docker", "ps", "-a", "--filter", f"name=prayog-alloc-{allocation_id}", "--format", "{{.Names}}"],
                                capture_output=True, text=True).stdout
        return bool(listed.strip())

    def test_a_run_in_a_container_ships_its_results_and_leaves_nothing_behind(self):
        run = self.submit_program()
        agent = self.make_agent(run, runner=self.docker_runner())
        self.drive(agent, self.finished(run), timeout=120)
        self.assertEqual(self.run_row(run).status, "SUCCEEDED", self.run_row(run).error_summary)
        shown = self.client.get(f"/mlflow/projects/{self.pid}/runs/A/1").json()
        self.assertEqual(sorted(c["step"] for c in shown["checkpoints"]), [1, 2, 3])
        self.assertEqual(self.artifact_bytes(shown["checkpoints"][1]["artifact_id"]), b"w2-lr0.5")
        self.assertIn("marker net-v1", self.log_text(run))
        self.assertFalse(self.container_exists(self.allocation.id))

    def test_the_container_is_isolated(self):
        run = self.submit_program(PROBE)
        self.drive(self.make_agent(run, runner=self.docker_runner()), self.finished(run), timeout=120)
        line = next(l for l in self.log_text(run).splitlines() if l.startswith("PROBE "))
        report = json.loads(line[len("PROBE "):])
        self.assertEqual(report["uid"], 65534)                                  # not root
        self.assertEqual(report["env_secrets"], [])                             # none of the platform's secrets
        for forbidden in ("write_code", "write_data", "write_root"):
            self.assertNotEqual(report[forbidden], "allowed", forbidden)       # code, data and root are read-only
        self.assertNotEqual(report["network"], "allowed")                       # no network
        self.assertEqual((report["write_output"], report["write_tmp"]), ("allowed", "allowed"))

    def test_a_crash_in_a_container_is_reported_with_its_exit_code(self):
        run = self.submit_program("import sys; print('boom'); sys.exit(7)\n", dataset=False)
        self.drive(self.make_agent(run, runner=self.docker_runner()), self.finished(run), timeout=120)
        row = self.run_row(run)
        self.assertEqual(row.status, "FAILED")
        self.assertIn("exited with code 7", row.error_summary)
        self.assertIn("boom", row.error_summary)

    def test_cancelling_stops_the_container(self):
        source = '''\
import os, pathlib, time
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
(out / "checkpoints").mkdir()
(out / "checkpoints" / "step-1.bin").write_bytes(b"saved")
time.sleep(600)
'''
        run = self.submit_program(source, dataset=False)
        agent = self.make_agent(run, runner=self.docker_runner())
        self.drive(agent, lambda: self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count() == 1, timeout=120)
        self.client.post(f"/jobs/{run['job_id']}/cancel")
        self.drive(agent, self.finished(run), timeout=120)
        self.assertEqual(self.run_row(run).status, "CANCELLED")
        self.assertFalse(self.container_exists(self.allocation.id))


if __name__ == "__main__":
    unittest.main()
