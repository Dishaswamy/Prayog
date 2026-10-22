"""The whole product, as a user meets it: the real `prayog` CLI talking real HTTP to the real server, with a real
node agent running the user's program. Create a project, upload a folder, run on the GPUs with a dataset, read the
results, update, switch versions, run again, restore. Needs the CLI installed (pip install -e ./cli)."""
import contextlib
import io
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from prayog import credentials
    from prayog.cli import main as prayog_main
except ImportError:                                       # the CLI is a separate package
    prayog_main = None

import uvicorn

import models as orm
from logic.mlflow import rebuildable_snapshot
from mlflow_models import ModelRun, ModelSnapshot
from node_agent import results
from node_agent.agent import NodeAgent
from node_agent.runners import SubprocessRunner
from node_agent.simulated import SimulatedExecutor
from tests.agent_case import TRAIN
from tests.gpu_case import GpuRunCase, requires_postgres


class LiveServer:
    """The real app on a local port, in a thread."""

    def __enter__(self):
        from main import app
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the test server did not start")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@unittest.skipIf(prayog_main is None, "the prayog CLI is not installed (pip install -e ./cli)")
@requires_postgres
class CliEndToEndTests(GpuRunCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(results, "STABLE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = self.make_user("cli-e2e")
        self.client_as(self.user)                                        # the server treats every request as this user
        live = LiveServer()
        self.live = live.__enter__()
        self.addCleanup(live.__exit__)
        import tempfile
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.config = patch.dict(os.environ, {"PRAYOG_CONFIG_DIR": str(self.home / "config")})
        self.config.start()
        self.addCleanup(self.config.stop)
        credentials.save(credentials.Credentials(api_url=self.live.url, token="not-checked", email=self.user.email,
                                                 user_id=self.user.user_id, role=self.user.role))
        self.folder = self.home / "my-project"
        (self.folder / "model").mkdir(parents=True)
        (self.folder / "data").mkdir()
        self.source = TRAIN.replace('"data/train.csv"', '"train.csv"')          # --data data/ puts the file at the data dir's root
        (self.folder / "main.py").write_text(self.source)
        (self.folder / "model" / "__init__.py").write_text("")
        (self.folder / "model" / "net.py").write_text("MARK = 'net-v1'\n")
        (self.folder / "data" / "train.csv").write_text("x,y\n1,2\n3,4\n5,6\n")
        (self.folder / ".env").write_text("SECRET=never-uploaded\n")

    def prayog(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prayog_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv) -> str:
        code, out, err = self.prayog(*argv)
        self.assertEqual(code, 0, f"prayog {' '.join(argv)} failed: {err}")
        return out

    def execute_next_job(self):
        """The cluster side: place the user's newest queued job on a node and let a real agent run it."""
        run = self.db.query(ModelRun).filter(ModelRun.user_id == self.user.id, ModelRun.status == "QUEUED") \
            .order_by(ModelRun.id.desc()).first()
        node, credential = self.make_agent_node()
        self.place(run.job_id, node)
        from fastapi.testclient import TestClient
        from main import app
        agent = NodeAgent(TestClient(app), self.home / f"agent-{run.id}", 2, SubprocessRunner(), executor=SimulatedExecutor())
        agent.state["credential"] = credential
        agent._save()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            agent.step()
            self.db.expire_all()
            if self.db.get(ModelRun, run.id).status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                return run.id
            time.sleep(0.05)
        self.fail("the run did not finish")

    def test_a_users_whole_journey(self):
        # 1. a project, and the folder uploaded as version A
        self.ok("project", "create", "vision")
        out = self.ok("project", "link", "vision", str(self.folder))
        self.assertIn("Uploaded version A of 'vision'", out)
        self.assertIn("1 dataset files fingerprinted, not uploaded", out)
        versions = self.ok("project", "versions", "vision")
        self.assertRegex(versions, r"A\s+current\s+head")

        # 2. a run with a dataset staged just for it, on 1 GPU for 10 minutes
        out = self.ok("run", "vision", "--gpus", "1", "--walltime", "00:10", "--data", str(self.folder / "data"),
                      "--hp", "lr=0.5")
        self.assertIn("uploaded 1, already on the server 0", out)
        self.assertIn("Queued A run 1", out)
        self.assertIn("QUEUED", self.ok("project", "status", "vision"))

        # 3. the cluster runs it; the user reads the results
        run_id = self.execute_next_job()
        status = self.ok("project", "status", "vision")
        self.assertIn("A run 1: SUCCEEDED (COMPLETED)", status)
        self.assertIn("checkpoints: 3 saved (steps 1 to 3), best at step 2", status)
        self.assertIn("rows=4", status)                                  # the program read the staged dataset
        self.assertIn("sealed as snapshot", status)
        self.assertIn("Temporary dataset copies held: 1 files", status)
        snapshot = self.db.query(ModelSnapshot).filter_by(run_id=run_id).one()
        self.assertIsNotNone(rebuildable_snapshot(self.db, snapshot.snapshot_id, self.user.id))

        # 3b. the user downloads what the run saved; every file is verified on the way
        saved = self.home / "results"
        self.ok("project", "download", "vision", "A/1", "--to", str(saved))
        self.assertEqual((saved / "checkpoints" / "step-2-best").read_bytes(), b"w2-lr0.5")     # the best checkpoint
        self.assertIn("marker net-v1", (saved / "run.log").read_text())
        self.assertIn('"name": "loss"', (saved / "metrics.json").read_text())
        self.ok("project", "download", "vision", "A/1", "--to", str(self.home / "all"), "--checkpoint", "all")
        self.assertEqual(sorted(p.name for p in (self.home / "all" / "checkpoints").iterdir()),
                         ["step-1", "step-2-best", "step-3"])

        # 4. a second run on the same data uploads nothing
        out = self.ok("run", "vision", "--walltime", "00:10", "--data", str(self.folder / "data"), "--hp", "lr=0.5")
        self.assertIn("uploaded 0, already on the server 1", out)
        self.execute_next_job()
        self.assertRegex(self.ok("project", "runs", "vision"), r"A run 2\s+SUCCEEDED")

        # 5. edit the code, update: version B becomes current; run it
        (self.folder / "model" / "net.py").write_text("MARK = 'net-v2'\n")
        self.assertIn("Uploaded version B", self.ok("project", "update", str(self.folder)))
        self.assertIn("Nothing changed since version B", self.ok("project", "update", str(self.folder)))
        self.ok("run", "vision", "--walltime", "00:10", "--data", str(self.folder / "data"), "--hp", "lr=0.5")
        self.execute_next_job()
        self.assertIn("B run 1: SUCCEEDED", self.ok("project", "status", "vision", "--run", "B/1"))

        # 6. go back to version A for the next run
        self.assertIn("now uses version A", self.ok("project", "checkout", "vision", "a"))
        self.assertIn("Current version of 'vision': A (older than the newest, B)", self.ok("project", "current", "vision"))
        self.assertIn("Queued A run 3", self.ok("run", "vision", "--walltime", "00:10", "--data", str(self.folder / "data"), "--hp", "lr=0.5"))
        runs = self.ok("project", "runs", "vision")
        self.assertRegex(runs, r"B run 1\s+SUCCEEDED")
        self.assertRegex(runs, r"A run 3\s+QUEUED")
        self.assertRegex(self.ok("project", "versions", "vision"), r"A\s+current\s+4\s+3\s+")     # 4 files (the .env is not one), runs 1, 2 and 3
        self.assertRegex(self.ok("project", "versions", "vision"), r"B\s+head\s+4\s+1\s+")

        # 7. restore version A exactly as uploaded; the secret was never stored
        restored = self.home / "restored-a"
        self.ok("project", "pull", "vision", str(restored), "--version", "A", "--data-dir", str(self.folder))
        self.assertEqual((restored / "model" / "net.py").read_text(), "MARK = 'net-v1'\n")
        self.assertEqual((restored / "main.py").read_text(), self.source)
        self.assertFalse((restored / ".env").exists())

        # 8. clean the temporary data now
        self.assertIn("Deleted", self.ok("project", "clean", "vision"))

    def test_rebuilding_a_run_and_checking_it_matches(self):
        data = str(self.folder / "data")
        self.ok("project", "create", "vision")
        self.ok("project", "link", "vision", str(self.folder))
        self.ok("run", "vision", "--walltime", "00:10", "--data", data, "--hp", "lr=0.5")
        self.execute_next_job()

        # the project moves on to version B, and the dataset file is edited
        (self.folder / "model" / "net.py").write_text("MARK = 'net-v2'\n")
        self.ok("project", "update", str(self.folder))
        (self.folder / "data" / "train.csv").write_text("x,y\n9,9\n")
        code, _, err = self.prayog("run", "vision", "--walltime", "00:10", "--rebuild", "A/1", "--data", data)
        self.assertEqual(code, 1)
        self.assertIn("different from the original: train.csv", err)                       # not the original's data
        self.assertEqual(self.db.query(ModelRun).filter_by(user_id=self.user.id).count(), 1)

        # with the original data back, it rebuilds version A (not the current B), with A run 1's settings
        (self.folder / "data" / "train.csv").write_text("x,y\n1,2\n3,4\n5,6\n")
        out = self.ok("run", "vision", "--walltime", "00:10", "--rebuild", "A/1", "--data", data)
        self.assertIn("Queued A run 2, a rebuild of A run 1", out)
        self.execute_next_job()
        compared = self.ok("project", "compare", "vision", "A/2")
        self.assertIn("A run 2 (SUCCEEDED) against A run 1", compared)
        self.assertIn("identical, byte for byte", compared)
        self.assertIn("Result: REPRODUCED", compared)
        self.assertIn("a rebuild of A run 1", self.ok("project", "status", "vision", "--run", "A/2"))
        code, _, err = self.prayog("project", "compare", "vision", "A/1")
        self.assertIn("not a rebuild", err)

    def test_errors_are_explained_not_dumped(self):
        self.ok("project", "create", "empty")
        code, _, err = self.prayog("run", "empty", "--walltime", "01:00")
        self.assertEqual(code, 1)
        self.assertIn("prayog project link", err)
        code, _, err = self.prayog("run", "nope", "--walltime", "01:00")
        self.assertIn("prayog project create nope", err)
        self.ok("project", "link", "empty", str(self.folder))
        code, _, err = self.prayog("run", "empty", "--walltime", "soon")
        self.assertEqual(code, 1)
        self.assertIn("Walltime must be HH:MM", err)
        code, _, err = self.prayog("project", "checkout", "empty", "Z")
        self.assertIn("no version Z", err)
        self.assertEqual(self.db.query(orm.Job).filter_by(user_id=self.user.id).count(), 0)


if __name__ == "__main__":
    unittest.main()
