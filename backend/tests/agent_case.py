"""Shared base for tests that run a real program through a real node agent against the real API."""
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import models as orm  # noqa: F401
from logic.mlflow_release import register_mlflow_hooks
from mlflow_models import ModelRun
from node_agent import results
from node_agent.agent import NodeAgent
from node_agent.runners import SubprocessRunner
from node_agent.simulated import SimulatedExecutor
from tests.gpu_case import GpuRunCase

TRAIN = '''\
import json, os, pathlib
from model.net import MARK

hp = json.loads(os.environ["PRAYOG_HYPERPARAMETERS_JSON"])
data = pathlib.Path(os.environ["PRAYOG_DATA_DIR"], "data/train.csv").read_text()
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
(out / "checkpoints").mkdir()
print("marker", MARK)
print("rows", len(data.splitlines()))
with open(out / "metrics.jsonl", "a") as metrics:
    for step in (1, 2, 3):
        metrics.write(json.dumps({"name": "loss", "value": 1.0 / step, "step": step}) + "\\n")
        metrics.write(json.dumps({"name": "rows", "value": len(data.splitlines()), "step": step}) + "\\n")
        tmp = out / "checkpoints" / f"step-{step}.tmp"
        tmp.write_bytes(f"w{step}-lr{hp['lr']}".encode())
        tmp.rename(out / "checkpoints" / (f"step-{step}-best.bin" if step == 2 else f"step-{step}.bin"))
'''


def program(body: str) -> bytes:
    return body.encode()


class AgentRunCase(GpuRunCase):
    def setUp(self):
        super().setUp()
        register_mlflow_hooks()
        patcher = patch.object(results, "STABLE_SECONDS", 0)       # do not wait for files to settle
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = self.make_user("e2e")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.pid = self.project["project_id"]
        self.rows = b"x,y\n1,2\n3,4\n5,6\n"
        self.stage(self.client, self.rows)
        self.state_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state_dir, ignore_errors=True))

    # ── helpers ───────────────────────────────────────────────────────────

    def submit_program(self, source: str = TRAIN, hyperparameters=None, dataset=True, **kw) -> dict:
        files = {"main.py": program(source), "model/__init__.py": b"", "model/net.py": b"MARK = 'net-v1'\n"}
        extra = {"dataset": [self.dataset_entry("data/train.csv", self.rows)]} if dataset else {}
        return self.queued_run(self.client, self.project, files, hyperparameters=hyperparameters or {"lr": 0.5},
                               **extra, **kw)

    def make_agent(self, run: dict, runner=None, clock=None, http=None):
        self.node, credential = self.make_agent_node()
        self.allocation = self.place(run["job_id"], self.node)
        from main import app
        from fastapi.testclient import TestClient
        agent = NodeAgent(http or TestClient(app), self.state_dir, 2, runner or SubprocessRunner(),
                          executor=SimulatedExecutor(), **({"clock": clock} if clock else {}))
        agent.state["credential"] = credential
        agent._save()
        return agent

    def drive(self, agent, until, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            agent.step()
            if until():
                return
            time.sleep(0.05)
        self.fail("the agent did not reach the expected state in time")

    def run_row(self, run) -> ModelRun:
        self.db.expire_all()
        return self.db.get(ModelRun, run["run_id"])

    def finished(self, run):
        return lambda: self.run_row(run).status in ("SUCCEEDED", "FAILED", "CANCELLED")

    def artifact_bytes(self, artifact_id: str) -> bytes:
        response = self.client.get(f"/mlflow/artifacts/{artifact_id}/content")
        self.assertEqual(response.status_code, 200)
        return response.content

    def log_text(self, run) -> str:
        return self.artifact_bytes(self.run_row(run).logs_artifact_id).decode()

