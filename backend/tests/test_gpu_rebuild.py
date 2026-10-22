"""Rebuilding a finished run: same version, hyperparameters and data (checked by BLAKE3), run again on the GPUs,
then compared with the original, metric by metric and weights byte for byte."""
import unittest
from datetime import UTC, datetime, timedelta

import models as orm
from mlflow_models import Artifact, ModelRun, ModelSnapshot, RunCheckpoint
from tests.agent_case import TRAIN, AgentRunCase
from tests.gpu_case import requires_postgres

NOISY = '''\
import json, os, pathlib
hp = json.loads(os.environ["PRAYOG_HYPERPARAMETERS_JSON"])
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
(out / "checkpoints").mkdir()
import random
jitter = random.random() * hp["scale"]
with open(out / "metrics.jsonl", "a") as m:
    m.write(json.dumps({"name": "loss", "value": 1.0 + jitter, "step": 1}) + "\\n")
(out / "checkpoints" / "step-1.bin").write_bytes(repr(jitter).encode())
'''


@requires_postgres
class GpuRebuildTests(AgentRunCase):
    def setUp(self):
        super().setUp()
        self.entry = self.dataset_entry("data/train.csv", self.rows)

    def finish(self, run) -> dict:
        self.drive(self.make_agent(run), self.finished(run))
        return self.client.get(f"/mlflow/projects/{self.pid}/runs/{run['version']}/{run['run_number']}").json()

    def original(self, source=TRAIN, hyperparameters=None, **kw) -> dict:
        run = self.submit_program(source, hyperparameters=hyperparameters or {"lr": 0.5}, **kw)
        done = self.finish(run)
        self.assertEqual(done["status"], "SUCCEEDED", done["error_summary"])
        return done

    def rebuild(self, of="A/1", data="same", **body):
        """Submit a rebuild. data: "same" sends the original's file, None sends none, or a list of dataset entries."""
        files = [self.entry] if data == "same" else (data or [])
        return self.submit(self.client, self.pid, rebuild_of=of, dataset=files, **body)

    def compare(self, ref="A/2", **params):
        version, number = ref.split("/")
        return self.client.get(f"/mlflow/projects/{self.pid}/runs/{version}/{number}/compare", params=params)

    # ── a faithful rebuild ────────────────────────────────────────────────

    def test_a_rebuild_reproduces_a_deterministic_run_byte_for_byte(self):
        original = self.original()
        response = self.rebuild()
        self.assertEqual(response.status_code, 201, response.text)
        queued = response.json()
        self.assertEqual((queued["label"], queued["rebuild_of"]), ("A run 2", "A run 1"))
        self.assertEqual(queued["hyperparameters"], original["hyperparameters"])
        done = self.finish(queued)
        self.assertEqual(done["status"], "SUCCEEDED")
        report = self.compare().json()
        self.assertEqual((report["original"], report["rebuilt"]), ("A run 1", "A run 2"))
        self.assertTrue(report["metrics_match"] and report["reproduced"])
        self.assertTrue(report["weights"]["identical"])
        self.assertEqual(report["weights"]["original_blake3"], report["weights"]["rebuilt_blake3"])
        self.assertEqual(len(report["weights"]["rebuilt_blake3"]), 64)
        self.assertEqual({(m["name"], m["step"]) for m in report["metrics"]},
                         {("loss", 1), ("loss", 2), ("loss", 3), ("rows", 1), ("rows", 2), ("rows", 3)})

    def test_the_rebuild_is_recorded_as_one_linked_to_its_snapshot_and_job(self):
        self.original()
        queued = self.rebuild().json()
        run = self.db.get(ModelRun, queued["run_id"])
        snapshot = self.db.query(ModelSnapshot).filter_by(run_id=self.db.query(ModelRun).filter_by(
            project_id=self.pid, run_number=1).one().id).one()
        self.assertEqual((run.run_type, run.source_snapshot_id), ("REBUILD", snapshot.snapshot_id))
        self.assertEqual(self.db.get(orm.Job, run.job_id).reproduces, f"reproduces:{snapshot.run_id}")

    def test_a_rebuild_uses_the_originals_version_even_after_the_project_moved_on(self):
        original = self.original()
        self.make_version(self.client, self.project, {"main.py": TRAIN.encode(), "model/__init__.py": b"",
                                                      "model/net.py": b"MARK = 'a completely different model'\n"})
        self.assertEqual(self.client.get(f"/mlflow/projects/{self.pid}/versions").json()[0]["version"], "B")
        queued = self.rebuild().json()
        self.assertEqual((queued["version"], queued["label"]), ("A", "A run 2"))
        self.finish(queued)
        self.assertIn("marker net-v1", self.log_text(queued))                    # version A's code, not B's
        self.assertEqual(original["version"], "A")

    def test_a_rebuild_takes_its_own_gpus_and_walltime(self):
        self.original()
        queued = self.rebuild(gpus=2, walltime="02:30").json()
        self.assertEqual((queued["gpus"], queued["walltime"]), (2, "02:30"))

    def test_rebuilding_a_rebuild_is_allowed(self):
        self.original()
        second = self.rebuild().json()
        self.finish(second)
        third = self.rebuild(of="A/2")
        self.assertEqual((third.status_code, third.json()["rebuild_of"]), (201, "A run 2"))

    def test_a_retried_rebuild_request_returns_the_same_run(self):
        self.original()
        first = self.rebuild(client_request_id="rb-1")
        again = self.rebuild(client_request_id="rb-1")
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json()["run_id"], again.json()["run_id"])

    # ── the comparison says no when it should ─────────────────────────────

    def test_a_non_deterministic_program_is_not_reproduced(self):
        self.original(NOISY, hyperparameters={"scale": 5.0}, dataset=False)
        queued = self.rebuild(data=None).json()
        self.finish(queued)
        report = self.compare().json()
        self.assertFalse(report["weights"]["identical"])
        self.assertFalse(report["metrics_match"])
        self.assertFalse(report["reproduced"])
        self.assertGreater(report["metrics"][0]["difference"], 0)
        self.assertFalse(report["metrics"][0]["within_tolerance"])

    def test_a_tolerance_accepts_small_differences(self):
        self.original(NOISY, hyperparameters={"scale": 1e-6}, dataset=False)
        queued = self.rebuild(data=None).json()
        self.finish(queued)
        strict, loose = self.compare(tolerance=0).json(), self.compare(tolerance=1e-3).json()
        self.assertFalse(strict["metrics_match"])                                # the difference is real but tiny
        self.assertTrue(loose["metrics_match"] and loose["reproduced"])
        self.assertFalse(loose["weights"]["identical"])                          # and the bytes still differ
        self.assertEqual((strict["tolerance"], loose["tolerance"]), (0.0, 1e-3))

    def test_a_metric_the_rebuild_never_logged_is_a_difference(self):
        original = self.original()
        queued = self.rebuild().json()
        self.finish(queued)
        from mlflow_models import RunMetric
        self.db.query(RunMetric).filter_by(run_id=queued["run_id"], name="rows", step=3).delete()
        self.db.commit()
        report = self.compare().json()
        missing = next(m for m in report["metrics"] if (m["name"], m["step"]) == ("rows", 3))
        self.assertEqual((missing["rebuilt"], missing["difference"], missing["within_tolerance"]), (None, None, False))
        self.assertFalse(report["metrics_match"] or report["reproduced"])
        self.assertEqual(original["status"], "SUCCEEDED")

    def test_a_rebuild_that_failed_is_not_reproduced(self):
        self.original()
        queued = self.rebuild().json()
        job = self.db.get(orm.Job, queued["job_id"])
        job.state = "FAILED"
        self.db.commit()
        from logic.mlflow_release import reconcile_runs
        reconcile_runs(self.db)
        report = self.compare().json()
        self.assertEqual((report["rebuilt_status"], report["reproduced"]), ("FAILED", False))

    def test_only_a_rebuild_can_be_compared_and_the_tolerance_must_be_sane(self):
        self.original()
        self.assertEqual(self.compare("A/1").status_code, 400)
        queued = self.rebuild().json()
        self.finish(queued)
        for bad in ("-1", "nan", "inf"):
            self.assertEqual(self.compare(tolerance=bad).status_code, 400, bad)
        self.assertEqual(self.compare("A/9").status_code, 404)

    # ── the data must be the original's ───────────────────────────────────

    def test_changed_data_is_refused_with_the_paths_named_and_nothing_is_queued(self):
        self.original()
        changed = self.rows + b"7,8\n"
        self.stage(self.client, changed)
        before = self.db.query(orm.Job).filter_by(user_id=self.user.id).count()
        response = self.rebuild(data=[self.dataset_entry("data/train.csv", changed)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "DATASET_MISMATCH")
        self.assertIn("different from the original: data/train.csv", response.json()["detail"])
        self.assertEqual(self.db.query(orm.Job).filter_by(user_id=self.user.id).count(), before)

    def test_missing_and_extra_data_files_are_named(self):
        self.original()
        extra = f"extra {datetime.now(UTC).isoformat()}".encode()
        self.stage(self.client, extra)
        missing = self.rebuild(data=None)
        self.assertEqual(missing.status_code, 409)
        self.assertIn("missing: data/train.csv", missing.json()["detail"])
        added = self.rebuild(data=[self.entry, self.dataset_entry("extra.csv", extra)])
        self.assertEqual(added.status_code, 409)
        self.assertIn("not in the original: extra.csv", added.json()["detail"])

    def test_the_data_must_be_staged_again_after_it_was_deleted(self):
        self.original()
        from logic import staging
        finished = self.db.query(ModelRun).filter_by(project_id=self.pid, run_number=1).one().finished_at
        staging.expire_staged_files(self.db, now=finished + timedelta(hours=49))
        response = self.rebuild()
        self.assertEqual((response.status_code, response.json()["code"]), (409, "DATASET_NOT_STAGED"))
        self.stage(self.client, self.rows)
        self.assertEqual(self.rebuild().status_code, 201)

    def test_a_run_without_data_is_rebuilt_without_data(self):
        self.original(NOISY, hyperparameters={"scale": 1.0}, dataset=False)
        self.assertEqual(self.rebuild(data=None).status_code, 201)
        stray = self.rebuild(data=[self.entry])
        self.assertEqual(stray.status_code, 409)
        self.assertIn("not in the original", stray.json()["detail"])

    # ── what may be rebuilt ───────────────────────────────────────────────

    def test_a_rebuild_may_not_change_the_originals_settings(self):
        self.original()
        for override in ({"hyperparameters": {"lr": 9}}, {"version": "A"}, {"entrypoint": "other.py"}):
            with self.subTest(override=override):
                self.assertEqual(self.rebuild(**override).status_code, 400)

    def test_unfinished_failed_and_unknown_runs_are_all_refused_the_same_way(self):
        pending = self.submit_program()                                          # A run 1: queued, never run
        refusals = []
        for ref in ("A/1", "A/7", "Z/1"):
            response = self.rebuild(of=ref)
            self.assertEqual((response.status_code, response.json()["code"]), (404, "REBUILD_REJECTED"), ref)
            refusals.append(response.json()["detail"])
        self.assertEqual(len(set(refusals)), 1)                                  # no hint which one exists
        self.finish(pending)
        self.assertEqual(self.rebuild(of="A/1").status_code, 201)

    def test_another_users_run_cannot_be_rebuilt(self):
        self.original()
        stranger = self.make_user("rb-stranger")
        other = self.client_for_other(stranger)
        theirs = self.make_project(other)
        self.make_version(other, theirs)
        response = other.post(f"/mlflow/projects/{theirs['project_id']}/runs",
                              json={"gpus": 1, "walltime": "01:00", "client_request_id": "x1", "rebuild_of": "A/1"})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "REBUILD_REJECTED"))

    def test_a_run_whose_stored_code_was_corrupted_cannot_be_rebuilt(self):
        self.original()
        version = self.client.get(f"/mlflow/projects/{self.pid}/versions").json()[0]
        manifest = self.db.get(Artifact, version["manifest_artifact_id"])
        (self.storage / manifest.storage_key).write_bytes(b"corrupted")
        response = self.rebuild()
        self.assertEqual((response.status_code, response.json()["code"]), (404, "REBUILD_REJECTED"))

    def test_malformed_references_are_bad_requests(self):
        self.original()
        for ref in ("A1", "A/", "/1", "A/0", "A/x"):
            with self.subTest(ref=ref):
                self.assertEqual(self.rebuild(of=ref).status_code, 400)

    def test_a_run_that_succeeded_without_saving_weights_compares_on_metrics_alone(self):
        self.original("""\
import json, os, pathlib
out = pathlib.Path(os.environ["PRAYOG_OUTPUT_DIR"])
open(out / "metrics.jsonl", "w").write(json.dumps({"name": "m", "value": 1.0, "step": 1}) + "\\n")
""", dataset=False)
        queued = self.rebuild(data=None).json()
        self.finish(queued)
        report = self.compare().json()
        self.assertEqual((report["weights"]["original_blake3"], report["weights"]["identical"]), (None, False))
        self.assertTrue(report["reproduced"])

    def test_the_rebuild_run_gets_its_own_checkpoints(self):
        self.original()
        queued = self.rebuild().json()
        self.finish(queued)
        self.assertEqual(self.db.query(RunCheckpoint).filter_by(run_id=queued["run_id"]).count(), 3)


if __name__ == "__main__":
    unittest.main()
