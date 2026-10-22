import unittest
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import models as orm
from logic import mlflow_release
from logic.mlflow import rebuildable_snapshot
from logic.mlflow_release import reconcile_runs, register_mlflow_hooks
from logic.scheduler import lifecycle
from logic.scheduler.domain import ReleaseReason
from mlflow_models import (
    MLFlowOutboxEvent, ModelRun, ModelSnapshot, RunCheckpoint, RunMetric, StagedFile,
)
from tests.gpu_case import GpuRunCase, requires_postgres


@requires_postgres
class RunFinalizeTests(GpuRunCase):
    """A GPU job's end reaches its ML Flow run: status, results, and a sealed snapshot on success."""

    def setUp(self):
        super().setUp()
        register_mlflow_hooks()
        self.user = self.make_user("fin")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.pid = self.project["project_id"]
        self.rows = f"x,y\n{uuid4().hex}\n".encode()
        self.stage(self.client, self.rows)
        self.run = self.queued_run(self.client, self.project, hyperparameters={"lr": 0.1},
                                   dataset=[self.dataset_entry("data/train.csv", self.rows)])
        self.node, self.credential = self.make_agent_node()
        self.allocation = self.place(self.run["job_id"], self.node)
        self.agent = self.agent_client(self.credential)
        self.aid = self.allocation.id

    # ── helpers ───────────────────────────────────────────────────────────

    def start(self):
        self.agent.post(f"/agent/allocations/{self.aid}/acknowledge")

    def train(self, steps=(1, 2, 3), best=2, log=True):
        self.start()
        self.agent.post(f"/agent/allocations/{self.aid}/metrics", json={"metrics": [
            {"name": "loss", "value": 1.0 / s, "step": s} for s in steps]})
        for s in steps:
            self.agent.post(f"/agent/allocations/{self.aid}/results", params={"kind": "WEIGHTS", "step": s,
                            "best": str(s == best).lower()}, content=f"weights-{s}".encode(),
                            headers={"Content-Type": "application/octet-stream"})
        if log:
            self.agent.post(f"/agent/allocations/{self.aid}/results", params={"kind": "LOG"}, content=b"epoch log\n",
                            headers={"Content-Type": "application/octet-stream"})

    def run_row(self) -> ModelRun:
        self.db.expire_all()
        return self.db.get(ModelRun, self.run["run_id"])

    def shown(self) -> dict:
        return self.client.get(f"/mlflow/projects/{self.pid}/runs/A/1").json()

    def snapshot(self):
        self.db.expire_all()
        return self.db.query(ModelSnapshot).filter_by(run_id=self.run["run_id"]).first()

    # ── success ───────────────────────────────────────────────────────────

    def test_success_marks_the_run_succeeded_and_keeps_every_result(self):
        self.train()
        response = self.report(self.agent, self.node, self.aid, "EXITED", 0)
        self.assertEqual(response.json(), {"applied": True, "job_state": "COMPLETED"})
        row = self.run_row()
        self.assertEqual((row.status, row.error_summary), ("SUCCEEDED", None))
        self.assertIsNotNone(row.finished_at)
        shown = self.shown()
        self.assertEqual((shown["status"], shown["job_state"]), ("SUCCEEDED", "COMPLETED"))
        self.assertEqual([c["step"] for c in shown["checkpoints"]], [1, 2, 3])
        self.assertEqual(shown["metrics"]["loss"], 1.0 / 3)
        self.assertIsNotNone(shown["logs_artifact_id"])

    def test_success_seals_a_snapshot_of_the_exact_version_with_the_best_checkpoint(self):
        from mlflow_models import Artifact, ProjectVersion
        self.train(best=2)
        self.report(self.agent, self.node, self.aid, "EXITED", 0)
        snapshot = self.snapshot()
        version = self.db.query(ProjectVersion).filter_by(project_id=self.pid).one()
        manifest = self.db.get(Artifact, version.manifest_artifact_id)
        self.assertEqual(snapshot.state, "RECORDED")
        self.assertEqual((snapshot.code_artifact_id, snapshot.code_sha256), (manifest.id, manifest.sha256))
        best = self.db.query(RunCheckpoint).filter_by(run_id=self.run["run_id"], is_best=True).one()
        self.assertEqual(snapshot.weights_artifact_id, best.artifact_id)
        self.assertEqual(snapshot.hyperparameters, {"lr": 0.1})
        self.assertEqual(snapshot.dependencies["project_version"], "A")
        self.assertEqual(snapshot.dependencies["manifest_hash"], version.manifest_hash)
        self.assertEqual(snapshot.dependencies["dataset"], [self.dataset_entry("data/train.csv", self.rows)])
        self.assertEqual(self.shown()["snapshot_id"], snapshot.snapshot_id)

    def test_without_a_best_flag_the_last_checkpoint_is_the_snapshot_weights(self):
        self.train(best=None)
        self.report(self.agent, self.node, self.aid, "EXITED", 0)
        last = self.db.query(RunCheckpoint).filter_by(run_id=self.run["run_id"], step=3).one()
        self.assertEqual(self.snapshot().weights_artifact_id, last.artifact_id)

    def test_a_successful_run_without_checkpoints_still_seals_without_weights(self):
        self.start()
        self.report(self.agent, self.node, self.aid, "EXITED", 0)
        snapshot = self.snapshot()
        self.assertEqual((snapshot.state, snapshot.weights_artifact_id), ("RECORDED", None))

    def test_the_sealed_snapshot_can_be_rebuilt(self):
        self.train()
        self.report(self.agent, self.node, self.aid, "EXITED", 0)
        self.assertIsNotNone(rebuildable_snapshot(self.db, self.snapshot().snapshot_id, self.user.id))

    def test_sealing_announces_the_run_once_to_the_ledger(self):
        self.train()
        self.report(self.agent, self.node, self.aid, "EXITED", 0)
        events = self.db.query(MLFlowOutboxEvent).filter_by(run_id=self.run["run_id"]).all()
        self.assertEqual([(e.event_type, e.status) for e in events], [("RUN_SEALED", "PENDING")])
        self.assertEqual(events[0].payload["snapshot_id"], self.snapshot().snapshot_id)

    def test_syncing_again_changes_nothing(self):
        self.train()
        self.report(self.agent, self.node, self.aid, "EXITED", 0)
        before = self.snapshot().snapshot_id
        self.assertFalse(mlflow_release.sync_run(self.db, self.run_row(), self.now))
        self.assertEqual(self.db.query(ModelSnapshot).filter_by(run_id=self.run["run_id"]).count(), 1)
        self.assertEqual(self.snapshot().snapshot_id, before)

    def test_the_datas_retention_clock_starts_when_the_run_ends(self):
        self.train()
        self.report(self.agent, self.node, self.aid, "EXITED", 0)
        finished = self.run_row().finished_at
        staged = self.db.query(StagedFile).filter_by(user_id=self.user.id, blake3=self.blake3_of(self.rows)).one()
        from logic import staging
        staging.expire_staged_files(self.db, now=finished + timedelta(hours=47))
        self.db.refresh(staged)
        self.assertIsNone(staged.deleted_at)
        staging.expire_staged_files(self.db, now=finished + timedelta(hours=49))
        self.db.refresh(staged)
        self.assertIsNotNone(staged.deleted_at)
        self.assertEqual(self.shown()["dataset"][0]["staged"], False)           # the fingerprint remains

    # ── failure and stops ─────────────────────────────────────────────────

    def test_a_crash_fails_the_run_with_the_exit_code_and_output_and_keeps_checkpoints(self):
        self.train(steps=(1, 2), best=None)
        response = self.report(self.agent, self.node, self.aid, "EXITED", 3, log_tail="Traceback ...\nCUDA out of memory")
        self.assertEqual(response.json()["job_state"], "FAILED")
        row = self.run_row()
        self.assertEqual(row.status, "FAILED")
        self.assertIn("exited with code 3", row.error_summary)
        self.assertIn("CUDA out of memory", row.error_summary)
        self.assertEqual(len(self.shown()["checkpoints"]), 2)
        self.assertEqual(row.weights_artifact_id, self.db.query(RunCheckpoint).filter_by(
            run_id=row.id, step=2).one().artifact_id)
        self.assertIsNone(self.snapshot())                                      # only a success is sealed
        self.assertEqual(self.db.query(MLFlowOutboxEvent).filter_by(run_id=row.id).count(), 0)

    def test_hitting_the_walltime_fails_the_run_and_keeps_its_checkpoints(self):
        self.train(steps=(1,), best=None)
        self.report(self.agent, self.node, self.aid, "WALLTIME", None)
        row = self.run_row()
        self.assertEqual(row.status, "FAILED")
        self.assertIn("walltime limit (01:00)", row.error_summary)
        self.assertEqual(self.shown()["job_state"], "WALLTIME_EXCEEDED")
        self.assertEqual(len(self.shown()["checkpoints"]), 1)

    def test_cancelling_a_running_job_cancels_the_run(self):
        self.train(steps=(1,), best=None)
        self.assertEqual(self.client.post(f"/jobs/{self.run['job_id']}/cancel").status_code, 200)
        self.assertEqual(self.run_row().status, "RUNNING")                      # until the agent confirms the stop
        self.report(self.agent, self.node, self.aid, "STOPPED", None)
        row = self.run_row()
        self.assertEqual(row.status, "CANCELLED")
        self.assertIn("Cancelled", row.error_summary)
        self.assertIsNone(self.snapshot())

    def test_a_job_the_platform_gives_up_on_fails_the_run_with_a_reason(self):
        """Never acknowledged, max_dispatch_attempts times over: the scheduler fails the job without a single run."""
        from logic.scheduler.config import DEFAULT_CONFIG
        for attempt in range(DEFAULT_CONFIG.max_dispatch_attempts):
            lifecycle.release(self.db, self.run["job_id"], ReleaseReason.DISPATCH_TIMEOUT)
            if attempt < DEFAULT_CONFIG.max_dispatch_attempts - 1:
                self.assertEqual(self.run_row().status, "QUEUED")
                self.place(self.run["job_id"], self.node)
        row = self.run_row()
        self.assertEqual(row.status, "FAILED")
        self.assertIn("could not be started", row.error_summary)
        self.assertIsNone(self.snapshot())

    # ── requeue ───────────────────────────────────────────────────────────

    def test_a_platform_stop_requeues_the_run_and_discards_the_partial_attempt(self):
        self.train()
        lifecycle.release(self.db, self.run["job_id"], ReleaseReason.NODE_FAILURE)
        row = self.run_row()
        self.assertEqual((row.status, row.started_at, row.logs_artifact_id, row.weights_artifact_id),
                         ("QUEUED", None, None, None))
        self.assertEqual(self.db.query(RunMetric).filter_by(run_id=row.id).count(), 0)
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=row.id).count(), 0)
        self.assertEqual(self.db.get(orm.Job, row.job_id).state, "QUEUED")

    def test_a_requeued_run_can_run_again_from_step_one(self):
        self.train()
        lifecycle.release(self.db, self.run["job_id"], ReleaseReason.NODE_FAILURE)
        other, credential = self.make_agent_node()
        second = self.place(self.run["job_id"], other)
        agent = self.agent_client(credential)
        agent.post(f"/agent/allocations/{second.id}/acknowledge")
        ok = agent.post(f"/agent/allocations/{second.id}/results", params={"kind": "WEIGHTS", "step": 1},
                        content=b"again", headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(self.run_row().status, "RUNNING")

    # ── upkeep ────────────────────────────────────────────────────────────

    def test_a_cancelled_queued_job_is_picked_up_by_the_sweep_and_shown_cancelled_at_once(self):
        waiting = self.submit(self.client, self.pid).json()                     # never placed on a node
        self.db.get(orm.Job, waiting["job_id"]).state = "QUEUED"
        self.db.commit()
        self.assertEqual(self.client.post(f"/jobs/{waiting['job_id']}/cancel").status_code, 200)
        self.db.expire_all()
        self.assertEqual(self.db.get(ModelRun, waiting["run_id"]).status, "QUEUED")      # no release happened
        shown = self.client.get(f"/mlflow/projects/{self.pid}/runs/A/{waiting['run_number']}").json()
        self.assertEqual(shown["status"], "CANCELLED")                                    # but the user sees it
        self.assertGreaterEqual(reconcile_runs(self.db), 1)
        self.db.expire_all()
        self.assertEqual(self.db.get(ModelRun, waiting["run_id"]).status, "CANCELLED")
        self.assertEqual(reconcile_runs(self.db), 0)

    def test_a_failing_hook_never_blocks_the_release(self):
        self.start()
        with patch.object(mlflow_release, "sync_run", side_effect=RuntimeError("boom")):
            self.report(self.agent, self.node, self.aid, "EXITED", 0)
        self.db.expire_all()
        self.assertEqual(self.db.get(orm.Job, self.run["job_id"]).state, "COMPLETED")
        self.assertEqual(self.run_row().status, "RUNNING")                      # not yet synced...
        reconcile_runs(self.db)
        self.assertEqual(self.run_row().status, "SUCCEEDED")                    # ...the sweep catches up

    def test_an_ordinary_job_is_untouched_by_the_hook(self):
        job = self.make_job(self.user)
        self.make_running(job, self.node)
        lifecycle.release(self.db, job.id, ReleaseReason.COMPLETED, exit_code=0)
        self.db.refresh(job)
        self.assertEqual(job.state, "COMPLETED")
        self.assertEqual(self.db.query(ModelRun).filter_by(job_id=job.id).count(), 0)


if __name__ == "__main__":
    unittest.main()
