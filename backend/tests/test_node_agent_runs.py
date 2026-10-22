"""A real program, run by a real agent, against the real API: code and data fetched and verified, results
shipped back, the run closed and sealed. Only the runner is the unisolated SubprocessRunner."""
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

import httpx

import models as orm
from mlflow_models import Artifact, ModelSnapshot, RunCheckpoint, RunMetric
from node_agent.agent import NodeAgent
from node_agent.runners import SubprocessRunner
from node_agent.simulated import SimulatedExecutor
from tests.agent_case import AgentRunCase
from tests.gpu_case import requires_postgres


@requires_postgres
class AgentRunsTests(AgentRunCase):
    # ── a whole run ───────────────────────────────────────────────────────

    def test_a_run_trains_ships_its_results_and_is_sealed(self):
        run = self.submit_program()
        agent = self.make_agent(run)
        self.drive(agent, self.finished(run))
        row = self.run_row(run)
        self.assertEqual((row.status, row.error_summary), ("SUCCEEDED", None))
        shown = self.client.get(f"/mlflow/projects/{self.pid}/runs/A/1").json()
        self.assertEqual(shown["job_state"], "COMPLETED")
        # what the program saw: the extracted multi-file code, the dataset, the hyperparameters
        log = self.log_text(run)
        self.assertIn("marker net-v1", log)
        self.assertIn("rows 4", log)
        self.assertEqual(shown["metrics"]["rows"], 4.0)
        self.assertEqual(shown["metrics"]["loss"], 1.0 / 3)
        # every checkpoint arrived with its exact bytes, and the best one is flagged
        checkpoints = {c["step"]: c for c in shown["checkpoints"]}
        self.assertEqual(sorted(checkpoints), [1, 2, 3])
        self.assertEqual([c["is_best"] for c in shown["checkpoints"]], [False, True, False])
        for step, checkpoint in checkpoints.items():
            self.assertEqual(self.artifact_bytes(checkpoint["artifact_id"]), f"w{step}-lr0.5".encode())
        # the sealed snapshot points at the exact code version and the best checkpoint
        snapshot = self.db.query(ModelSnapshot).filter_by(run_id=run["run_id"]).one()
        self.assertEqual((snapshot.state, snapshot.weights_artifact_id), ("RECORDED", checkpoints[2]["artifact_id"]))
        self.assertEqual(self.db.get(orm.Job, run["job_id"]).state, "COMPLETED")

    def test_the_workspace_is_removed_and_nothing_is_run_twice(self):
        run = self.submit_program()
        agent = self.make_agent(run)
        self.drive(agent, self.finished(run))
        agent.step()
        agent.step()
        self.assertEqual(list((self.state_dir / "workspaces").iterdir()), [])
        self.assertEqual(agent.state["running"], {})
        self.assertEqual(self.db.query(RunMetric).filter_by(run_id=run["run_id"], name="rows").count(), 3)
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count(), 3)

    def test_results_arrive_while_the_program_is_still_running(self):
        go = self.state_dir / "go"
        source = '''\
import json, os, pathlib, time
hp = json.loads(os.environ["PRAYOG_HYPERPARAMETERS_JSON"])
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
(out / "checkpoints").mkdir()
(out / "checkpoints" / "step-1.bin").write_bytes(b"first")
(out / "metrics.jsonl").write_text(json.dumps({"name": "loss", "value": 0.9, "step": 1}) + "\\n")
while not pathlib.Path(hp["go"]).exists():
    time.sleep(0.05)
(out / "checkpoints" / "step-2.bin").write_bytes(b"second")
'''
        run = self.submit_program(source, hyperparameters={"go": str(go)}, dataset=False)
        agent = self.make_agent(run)
        self.drive(agent, lambda: self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count() == 1)
        self.assertEqual(self.run_row(run).status, "RUNNING")                  # still training
        self.assertEqual(self.client.get(f"/mlflow/projects/{self.pid}/runs/A/1").json()["metrics"], {"loss": 0.9})
        go.write_text("go")
        self.drive(agent, self.finished(run))
        self.assertEqual(self.run_row(run).status, "SUCCEEDED")
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count(), 2)

    def test_a_checkpoint_still_being_written_is_not_shipped_early(self):
        go = self.state_dir / "go2"
        source = '''\
import json, os, pathlib, time
hp = json.loads(os.environ["PRAYOG_HYPERPARAMETERS_JSON"])
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
(out / "checkpoints").mkdir()
(out / "checkpoints" / "step-1.tmp").write_bytes(b"half")
while not pathlib.Path(hp["go"]).exists():
    time.sleep(0.05)
(out / "checkpoints" / "step-1.tmp").rename(out / "checkpoints" / "step-1.bin")
'''
        run = self.submit_program(source, hyperparameters={"go": str(go)}, dataset=False)
        agent = self.make_agent(run)
        for _ in range(15):
            agent.step()
            time.sleep(0.05)
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count(), 0)
        go.write_text("go")
        self.drive(agent, self.finished(run))
        stored = self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).one()
        self.assertEqual(self.artifact_bytes(stored.artifact_id), b"half")

    # ── failure and stops ─────────────────────────────────────────────────

    def test_a_crashing_program_fails_the_run_and_keeps_what_it_saved(self):
        source = '''\
import os, pathlib, sys
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
(out / "checkpoints").mkdir()
(out / "checkpoints" / "step-1.bin").write_bytes(b"before the crash")
print("about to fail")
raise SystemExit(3)
'''
        run = self.submit_program(source, dataset=False)
        self.drive(self.make_agent(run), self.finished(run))
        row = self.run_row(run)
        self.assertEqual(row.status, "FAILED")
        self.assertIn("exited with code 3", row.error_summary)
        self.assertIn("about to fail", row.error_summary)
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count(), 1)
        self.assertIn("about to fail", self.log_text(run))
        self.assertIsNone(self.db.query(ModelSnapshot).filter_by(run_id=run["run_id"]).first())

    def test_cancelling_stops_the_program_and_cancels_the_run(self):
        source = '''\
import os, pathlib, time
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
(out / "checkpoints").mkdir()
(out / "checkpoints" / "step-1.bin").write_bytes(b"saved")
print("training", flush=True)
time.sleep(600)
'''
        run = self.submit_program(source, dataset=False)
        agent = self.make_agent(run)
        self.drive(agent, lambda: self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count() == 1)
        self.assertEqual(self.client.post(f"/jobs/{run['job_id']}/cancel").status_code, 200)
        self.drive(agent, self.finished(run))
        self.assertEqual(self.run_row(run).status, "CANCELLED")
        self.assertEqual(self.db.get(orm.Job, run["job_id"]).state, "CANCELLED")
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count(), 1)   # kept
        self.assertIn("training", self.log_text(run))

    def test_the_agent_stops_a_run_at_its_walltime_and_keeps_its_checkpoints(self):
        source = '''\
import os, pathlib, time
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
(out / "checkpoints").mkdir()
(out / "checkpoints" / "step-1.bin").write_bytes(b"saved")
time.sleep(600)
'''
        run = self.submit_program(source, dataset=False)
        offset = {"minutes": 0}
        from datetime import UTC, datetime
        clock = lambda: datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=offset["minutes"])
        agent = self.make_agent(run, clock=clock)
        self.drive(agent, lambda: self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count() == 1)
        offset["minutes"] = 61                                                  # the one-hour walltime passes
        self.drive(agent, self.finished(run))
        row = self.run_row(run)
        self.assertEqual(row.status, "FAILED")
        self.assertIn("walltime limit", row.error_summary)
        self.assertEqual(self.db.get(orm.Job, run["job_id"]).state, "WALLTIME_EXCEEDED")
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count(), 1)

    # ── nothing unverified runs ───────────────────────────────────────────

    def test_corrupted_code_is_never_run(self):
        marker = self.state_dir / "ran"
        run = self.submit_program(f"open({str(marker)!r}, 'w').write('x')\n", dataset=False)
        chunk = self.db.query(Artifact).filter_by(project_id=self.pid, kind="CODE").order_by(Artifact.created_at).first()
        (self.storage / chunk.storage_key).write_bytes(b"not the archive")
        self.drive(self.make_agent(run), self.finished(run))
        row = self.run_row(run)
        self.assertEqual(row.status, "FAILED")
        self.assertIn("exited with code 125", row.error_summary)
        self.assertIn("could not be started", self.log_text(run))
        self.assertFalse(marker.exists())

    def test_a_corrupted_dataset_file_is_never_trained_on(self):
        marker = self.state_dir / "ran2"
        run = self.submit_program(f"open({str(marker)!r}, 'w').write('x')\n")
        (self.storage / self.blake3_of(self.rows)).write_bytes(b"x,y\n9,9\n")
        self.drive(self.make_agent(run), self.finished(run))
        self.assertEqual(self.run_row(run).status, "FAILED")
        self.assertFalse(marker.exists())

    def test_a_dataset_deleted_before_the_run_started_fails_it_clearly(self):
        run = self.submit_program()
        from mlflow_models import StagedFile
        from datetime import datetime
        self.db.query(StagedFile).filter_by(user_id=self.user.id, blake3=self.blake3_of(self.rows)) \
            .update({"deleted_at": datetime.utcnow()})
        self.db.commit()
        self.drive(self.make_agent(run), self.finished(run))
        row = self.run_row(run)
        self.assertEqual(row.status, "FAILED")
        self.assertIn("could not be started", self.log_text(run))

    # ── resilience and compatibility ──────────────────────────────────────

    def test_a_plain_job_without_a_run_is_handled_as_before(self):
        job = self.make_job(self.user)
        node, credential = self.make_agent_node()
        self.place(job.id, node)
        from main import app
        from fastapi.testclient import TestClient
        agent = NodeAgent(TestClient(app), self.state_dir, 2, SubprocessRunner(),
                          executor=SimulatedExecutor(run_seconds=0))
        agent.state["credential"] = credential
        agent._save()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            agent.step()
            self.db.expire_all()
            if self.db.get(orm.Job, job.id).state == "COMPLETED":
                break
            time.sleep(0.05)
        self.assertEqual(self.db.get(orm.Job, job.id).state, "COMPLETED")

    def test_the_final_results_are_retried_when_the_server_is_unreachable(self):
        from main import app
        from fastapi.testclient import TestClient
        outage = {"left": 3}

        class Flaky(TestClient):
            def post(self, url, *args, **kwargs):
                if str(url).endswith("/results") and outage["left"] > 0:
                    outage["left"] -= 1
                    raise httpx.ConnectError("server unreachable")
                return super().post(url, *args, **kwargs)

        run = self.submit_program()
        agent = self.make_agent(run, http=Flaky(app))
        self.drive(agent, self.finished(run))
        self.assertEqual(outage["left"], 0)
        self.assertEqual(self.run_row(run).status, "SUCCEEDED")
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=run["run_id"]).count(), 3)   # each once

    def test_a_restarted_agent_finds_the_run_and_does_not_start_it_again(self):
        starts = []

        class Fake:
            def __init__(self):
                self.polls = 0

            def start(self, spec):
                starts.append(spec.name)
                (spec.output_dir / "checkpoints").mkdir()
                (spec.output_dir / "checkpoints" / "step-1.bin").write_bytes(b"w")
                spec.log_path.write_text("hello from fake\n")
                return {"kind": "fake", "name": spec.name}

            def poll(self, handle):
                self.polls += 1
                return 0 if self.polls > 2 else None

            def stop(self, handle): ...

            def cleanup(self, handle): ...

        run = self.submit_program(dataset=False)
        fake = Fake()
        agent = self.make_agent(run, runner=fake)
        agent.step()                                            # starts it
        self.assertEqual(starts, ["alloc-%d" % self.allocation.id])
        from main import app
        from fastapi.testclient import TestClient
        restarted = NodeAgent(TestClient(app), self.state_dir, 2, fake, executor=SimulatedExecutor())
        self.drive(restarted, self.finished(run))
        self.assertEqual(len(starts), 1)
        self.assertEqual(self.run_row(run).status, "SUCCEEDED")

    def test_an_agent_that_died_while_preparing_fails_the_run_instead_of_hanging(self):
        marker = self.state_dir / "ran3"
        run = self.submit_program(f"open({str(marker)!r}, 'w').write('x')\n", dataset=False)
        agent = self.make_agent(run)
        with patch("node_agent.agent.prepare", side_effect=KeyboardInterrupt):        # killed mid-preparation
            with self.assertRaises(KeyboardInterrupt):
                agent.step()
        from main import app
        from fastapi.testclient import TestClient
        restarted = NodeAgent(TestClient(app), self.state_dir, 2, SubprocessRunner(), executor=SimulatedExecutor())
        self.drive(restarted, self.finished(run))
        row = self.run_row(run)
        self.assertEqual(row.status, "FAILED")
        self.assertIn("restarted while preparing", self.log_text(run))
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
