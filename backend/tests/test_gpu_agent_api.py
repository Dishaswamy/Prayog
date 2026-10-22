import unittest
from uuid import uuid4

from logic.scheduler import lifecycle
from logic.scheduler.domain import ReleaseReason
from mlflow_models import ModelRun, RunCheckpoint, RunMetric
from tests.gpu_case import GpuRunCase, requires_postgres


@requires_postgres
class AgentWorkloadTests(GpuRunCase):
    """What a node agent may read for the allocation it was given, and nothing else."""

    def setUp(self):
        super().setUp()
        self.user = self.make_user("agw")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.pid = self.project["project_id"]
        self.rows = f"x,y\n{uuid4().hex}\n".encode()
        self.stage(self.client, self.rows)
        self.files = {"main.py": b"print('train')\n", "model/net.py": b"class Net: ...\n"}
        self.run = self.queued_run(self.client, self.project, self.files, hyperparameters={"lr": 0.05},
                                   dataset=[self.dataset_entry("data/train.csv", self.rows)])
        self.node, self.credential = self.make_agent_node()
        self.allocation = self.place(self.run["job_id"], self.node)
        self.agent = self.agent_client(self.credential)
        self.aid = self.allocation.id

    def test_the_workload_describes_the_run(self):
        response = self.agent.get(f"/agent/allocations/{self.aid}/workload")
        self.assertEqual(response.status_code, 200, response.text)
        load = response.json()
        self.assertEqual((load["run_id"], load["label"], load["entrypoint"]), (self.run["run_id"], "A run 1", "main.py"))
        self.assertEqual((load["hyperparameters"], load["walltime_minutes"]), ({"lr": 0.05}, 60))
        self.assertEqual(sorted(f["path"] for f in load["files"]), sorted(self.files))
        self.assertEqual(load["dataset"], [self.dataset_entry("data/train.csv", self.rows)])
        self.assertEqual(len(load["chunks"]), 1)

    def test_the_code_archive_can_be_downloaded_chunk_by_chunk(self):
        load = self.agent.get(f"/agent/allocations/{self.aid}/workload").json()
        body = b"".join(self.agent.get(f"/agent/allocations/{self.aid}/code/{c['artifact_id']}").content
                        for c in load["chunks"])
        import hashlib
        self.assertEqual(hashlib.sha256(body).hexdigest(), load["archive_sha256"])
        self.assertEqual(len(body), load["archive_size_bytes"])
        manifest = self.agent.get(f"/agent/allocations/{self.aid}/code/{load['manifest_artifact_id']}")
        self.assertEqual(manifest.status_code, 200)

    def test_code_outside_the_runs_version_is_not_served(self):
        unrelated = self.upload(self.client, self.project["experiment_id"], "CODE", b"someone's other file")
        response = self.agent.get(f"/agent/allocations/{self.aid}/code/{unrelated['id']}")
        self.assertEqual(response.status_code, 404)
        weights = self.upload(self.client, self.project["experiment_id"], "WEIGHTS", b"w")
        self.assertEqual(self.agent.get(f"/agent/allocations/{self.aid}/code/{weights['id']}").status_code, 404)

    def test_a_dataset_file_is_served_with_its_exact_bytes(self):
        response = self.agent.get(f"/agent/allocations/{self.aid}/data/{self.blake3_of(self.rows)}")
        self.assertEqual((response.status_code, response.content), (200, self.rows))
        self.assertIn("no-store", response.headers["cache-control"])

    def test_a_staged_file_that_is_not_in_this_runs_dataset_is_not_served(self):
        other = f"other {uuid4().hex}".encode()
        self.stage(self.client, other)                          # the same user's, but not this run's
        self.assertEqual(self.agent.get(f"/agent/allocations/{self.aid}/data/{self.blake3_of(other)}").status_code, 404)

    def test_deleted_data_is_not_served(self):
        from mlflow_models import StagedFile
        from datetime import datetime
        row = self.db.query(StagedFile).filter_by(user_id=self.user.id, blake3=self.blake3_of(self.rows)).one()
        row.deleted_at = datetime.utcnow()
        self.db.commit()
        self.assertEqual(self.agent.get(f"/agent/allocations/{self.aid}/data/{self.blake3_of(self.rows)}").status_code, 404)

    def test_corrupted_staged_bytes_are_refused_not_served(self):
        (self.storage / self.blake3_of(self.rows)).write_bytes(b"tampered")
        response = self.agent.get(f"/agent/allocations/{self.aid}/data/{self.blake3_of(self.rows)}")
        self.assertEqual(response.status_code, 400)

    def test_another_nodes_agent_cannot_read_the_allocation(self):
        _, other_credential = self.make_agent_node()
        other = self.agent_client(other_credential)
        for path in ("workload", f"code/{self.run['run_id']}", f"data/{self.blake3_of(self.rows)}"):
            self.assertEqual(other.get(f"/agent/allocations/{self.aid}/{path}").status_code, 404, path)

    def test_no_credential_or_a_wrong_one_is_unauthorised(self):
        from main import app
        from fastapi.testclient import TestClient
        self.assertEqual(TestClient(app).get(f"/agent/allocations/{self.aid}/workload").status_code, 401)
        self.assertEqual(self.agent_client("not-a-credential").get(f"/agent/allocations/{self.aid}/workload").status_code, 401)

    def test_a_released_allocation_serves_nothing(self):
        lifecycle.release(self.db, self.run["job_id"], ReleaseReason.DISPATCH_TIMEOUT)
        self.assertEqual(self.agent.get(f"/agent/allocations/{self.aid}/workload").status_code, 409)

    def test_a_job_that_is_not_an_ml_run_has_no_workload(self):
        job = self.make_job(self.user)
        allocation = self.place(job.id, self.node)
        self.assertEqual(self.agent.get(f"/agent/allocations/{allocation.id}/workload").status_code, 404)


@requires_postgres
class AgentResultsTests(GpuRunCase):
    """Metrics, checkpoints and logs sent back by the agent of a started allocation."""

    def setUp(self):
        super().setUp()
        self.user = self.make_user("agr")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.run = self.queued_run(self.client, self.project)
        self.node, self.credential = self.make_agent_node()
        self.allocation = self.place(self.run["job_id"], self.node)
        self.agent = self.agent_client(self.credential)
        self.aid = self.allocation.id

    def start(self):
        self.assertEqual(self.agent.post(f"/agent/allocations/{self.aid}/acknowledge").json(), {"started": True})

    def checkpoint(self, payload: bytes, step, best=False, kind="WEIGHTS"):
        params = {"kind": kind, **({} if step is None else {"step": step}), "best": str(best).lower()}
        return self.agent.post(f"/agent/allocations/{self.aid}/results", params=params, content=payload,
                               headers={"Content-Type": "application/octet-stream"})

    def run_row(self) -> ModelRun:
        self.db.expire_all()
        return self.db.get(ModelRun, self.run["run_id"])

    def test_acknowledging_shows_the_run_as_running(self):
        self.assertEqual(self.run_row().status, "QUEUED")
        self.start()
        row = self.run_row()
        self.assertEqual(row.status, "RUNNING")
        self.assertIsNotNone(row.started_at)

    def test_results_are_refused_before_the_allocation_has_started(self):
        self.assertEqual(self.checkpoint(b"w", 1).status_code, 409)
        body = {"metrics": [{"name": "loss", "value": 1.0, "step": 0}]}
        self.assertEqual(self.agent.post(f"/agent/allocations/{self.aid}/metrics", json=body).status_code, 409)

    def test_metrics_are_recorded_against_the_run(self):
        self.start()
        body = {"metrics": [{"name": "loss", "value": 0.9, "step": 0}, {"name": "loss", "value": 0.5, "step": 1},
                            {"name": "acc", "value": 0.7, "step": 1}]}
        response = self.agent.post(f"/agent/allocations/{self.aid}/metrics", json=body)
        self.assertEqual((response.status_code, response.json()), (201, {"recorded": 3}))
        shown = self.client.get(f"/mlflow/projects/{self.project['project_id']}/runs/A/1").json()
        self.assertEqual(shown["metrics"], {"loss": 0.5, "acc": 0.7})          # the last value of each

    def test_a_repeated_metric_step_is_a_conflict(self):
        self.start()
        body = {"metrics": [{"name": "loss", "value": 0.9, "step": 0}]}
        self.agent.post(f"/agent/allocations/{self.aid}/metrics", json=body)
        self.assertEqual(self.agent.post(f"/agent/allocations/{self.aid}/metrics", json=body).status_code, 409)

    def test_bad_metrics_are_refused(self):
        self.start()
        for body in ({"metrics": []}, {"metrics": [{"name": "", "value": 1}]},
                     {"metrics": [{"name": "x", "value": 1, "step": -1}]}):
            self.assertEqual(self.agent.post(f"/agent/allocations/{self.aid}/metrics", json=body).status_code, 422)

    def test_checkpoints_are_stored_as_the_runs_weights_artifacts(self):
        self.start()
        for step, payload in ((1, b"w-one"), (2, b"w-two"), (3, b"w-three")):
            response = self.checkpoint(payload, step, best=(step == 2))
            self.assertEqual(response.status_code, 201, response.text)
        shown = self.client.get(f"/mlflow/projects/{self.project['project_id']}/runs/A/1").json()
        self.assertEqual([(c["step"], c["is_best"], c["size_bytes"]) for c in shown["checkpoints"]],
                         [(1, False, 5), (2, True, 5), (3, False, 7)])
        stored = self.db.query(RunCheckpoint).filter_by(run_id=self.run["run_id"]).count()
        self.assertEqual(stored, 3)

    def test_a_new_best_replaces_the_old_one(self):
        self.start()
        self.checkpoint(b"a", 1, best=True)
        self.checkpoint(b"b", 2, best=True)
        rows = {c.step: c.is_best for c in self.db.query(RunCheckpoint).filter_by(run_id=self.run["run_id"])}
        self.assertEqual(rows, {1: False, 2: True})

    def test_a_step_is_checkpointed_once(self):
        self.start()
        self.checkpoint(b"a", 1)
        self.assertEqual(self.checkpoint(b"b", 1).status_code, 409)

    def test_a_checkpoint_needs_a_step_and_cannot_be_code(self):
        self.start()
        self.assertEqual(self.checkpoint(b"a", None).status_code, 400)
        self.assertEqual(self.checkpoint(b"a", -1).status_code, 400)
        self.assertEqual(self.checkpoint(b"a", 1, kind="CODE").status_code, 422)

    def test_an_empty_checkpoint_is_refused(self):
        self.start()
        self.assertEqual(self.checkpoint(b"", 1).status_code, 400)
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=self.run["run_id"]).count(), 0)

    def test_two_checkpoints_with_identical_bytes_share_storage_but_keep_their_steps(self):
        self.start()
        self.checkpoint(b"same", 1)
        self.checkpoint(b"same", 2)
        rows = self.db.query(RunCheckpoint).filter_by(run_id=self.run["run_id"]).order_by(RunCheckpoint.step).all()
        self.assertEqual([r.step for r in rows], [1, 2])
        self.assertEqual(rows[0].artifact_id, rows[1].artifact_id)

    def test_the_log_is_attached_to_the_run(self):
        self.start()
        response = self.checkpoint(b"epoch 1\nepoch 2\n", None, kind="LOG")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.run_row().logs_artifact_id, response.json()["artifact_id"])

    def test_artifacts_belong_to_the_runs_owner_and_experiment(self):
        from mlflow_models import Artifact
        self.start()
        artifact = self.db.get(Artifact, self.checkpoint(b"owned", 1).json()["artifact_id"])
        self.assertEqual((artifact.user_id, artifact.experiment_id, artifact.project_id, artifact.kind),
                         (self.user.id, self.project["experiment_id"], self.project["project_id"], "WEIGHTS"))

    def test_another_nodes_agent_cannot_send_results(self):
        self.start()
        _, other_credential = self.make_agent_node()
        other = self.agent_client(other_credential)
        response = other.post(f"/agent/allocations/{self.aid}/results", params={"kind": "WEIGHTS", "step": 1},
                              content=b"w", headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(response.status_code, 404)
        body = {"metrics": [{"name": "loss", "value": 1.0}]}
        self.assertEqual(other.post(f"/agent/allocations/{self.aid}/metrics", json=body).status_code, 404)
        self.assertEqual(self.db.query(RunMetric).filter_by(run_id=self.run["run_id"]).count(), 0)

    def test_results_are_refused_after_the_allocation_is_released(self):
        self.start()
        lifecycle.release(self.db, self.run["job_id"], ReleaseReason.COMPLETED, exit_code=0)
        self.assertEqual(self.checkpoint(b"late", 9).status_code, 409)


if __name__ == "__main__":
    unittest.main()
