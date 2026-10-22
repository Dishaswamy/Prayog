import unittest

import models as orm
from mlflow_models import ModelRun, RunDatasetFile
from tests.gpu_case import GpuRunCase, requires_postgres


@requires_postgres
class RunSubmissionTests(GpuRunCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("sub")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.pid = self.project["project_id"]

    def jobs(self):
        self.db.expire_all()
        return self.db.query(orm.Job).filter_by(user_id=self.user.id).all()

    def test_a_run_is_numbered_within_its_version_and_linked_to_a_job(self):
        version = self.make_version(self.client, self.project)
        response = self.submit(self.client, self.pid, gpus=2, walltime="04:00", hyperparameters={"lr": 0.01})
        self.assertEqual(response.status_code, 201, response.text)
        run = response.json()
        self.assertEqual((run["label"], run["version"], run["run_number"]), ("A run 1", "A", 1))
        self.assertEqual((run["gpus"], run["walltime"], run["status"]), (2, "04:00", "QUEUED"))
        self.assertEqual(run["hyperparameters"], {"lr": 0.01})
        job = self.db.get(orm.Job, run["job_id"])
        self.assertEqual((job.project_ref, job.snapshot_hash, job.gpus, job.walltime_minutes),
                         (self.pid, version["manifest_hash"], 2, 240))

    def test_runs_count_up_within_a_version_and_restart_in_the_next(self):
        self.make_version(self.client, self.project, {"main.py": b"v1\n"})
        labels = [self.submit(self.client, self.pid).json()["label"] for _ in range(2)]
        self.make_version(self.client, self.project, {"main.py": b"v2\n"})
        labels.append(self.submit(self.client, self.pid).json()["label"])
        labels.append(self.submit(self.client, self.pid, version="a").json()["label"])   # explicit older version
        self.assertEqual(labels, ["A run 1", "A run 2", "B run 1", "A run 3"])

    def test_a_run_uses_the_current_version_unless_told_otherwise(self):
        a = self.make_version(self.client, self.project, {"main.py": b"v1\n"})
        self.make_version(self.client, self.project, {"main.py": b"v2\n"})
        self.client.put(f"/mlflow/projects/{self.pid}/current", json={"version": "A"})
        run = self.submit(self.client, self.pid).json()
        self.assertEqual(run["version"], "A")
        self.assertEqual(self.db.get(orm.Job, run["job_id"]).snapshot_hash, a["manifest_hash"])

    def test_a_retry_returns_the_same_run_and_creates_nothing_more(self):
        self.make_version(self.client, self.project)
        first = self.submit(self.client, self.pid, client_request_id="retry-1")
        again = self.submit(self.client, self.pid, client_request_id="retry-1")
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json()["run_id"], again.json()["run_id"])
        self.assertFalse(again.json()["created"])
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.db.query(ModelRun).filter_by(project_id=self.pid).count(), 1)

    def test_a_retry_after_a_crash_between_job_and_run_creates_the_missing_run(self):
        self.make_version(self.client, self.project)
        first = self.submit(self.client, self.pid, client_request_id="crash-1").json()
        run = self.db.get(ModelRun, first["run_id"])
        self.db.delete(run)                                    # as if the process died after the job was queued
        self.db.commit()
        again = self.submit(self.client, self.pid, client_request_id="crash-1")
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual((again.json()["job_id"], again.json()["run_number"]), (first["job_id"], 1))
        self.assertEqual(len(self.jobs()), 1)

    def test_no_uploaded_version_is_not_found(self):
        response = self.submit(self.client, self.pid)
        self.assertEqual(response.status_code, 404)
        self.assertIn("prayog project link", response.json()["detail"])
        self.assertEqual(self.jobs(), [])

    def test_an_unknown_version_is_not_found_and_queues_nothing(self):
        self.make_version(self.client, self.project)
        self.assertEqual(self.submit(self.client, self.pid, version="D").status_code, 404)
        self.assertEqual(self.jobs(), [])

    def test_an_entrypoint_the_version_lacks_is_refused_before_a_job_exists(self):
        self.make_version(self.client, self.project, {"main.py": b"x\n", "train.py": b"y\n"})
        self.assertEqual(self.submit(self.client, self.pid, entrypoint="other.py").status_code, 400)
        self.assertEqual(self.submit(self.client, self.pid, entrypoint="notes.txt").status_code, 400)
        self.assertEqual(self.submit(self.client, self.pid, entrypoint="../main.py").status_code, 400)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.submit(self.client, self.pid, entrypoint="train.py").status_code, 201)

    def test_scheduler_rules_still_apply_and_no_run_is_left_behind(self):
        self.make_version(self.client, self.project)
        bad_walltime = self.submit(self.client, self.pid, walltime="soon")
        self.assertEqual(bad_walltime.json()["code"], "INVALID_WALLTIME")
        self.assertEqual(self.submit(self.client, self.pid, gpus=0).status_code, 400)
        self.assertEqual(self.db.query(ModelRun).filter_by(project_id=self.pid).count(), 0)
        self.assertEqual(self.jobs(), [])

    def test_a_user_without_gpu_access_is_refused(self):
        outsider = self.make_user("noaccess", access=False)
        client = self.client_for_other(outsider)
        project = self.make_project(client)
        self.make_version(client, project)
        response = self.submit(client, project["project_id"])
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "AUTHORIZATION_INACTIVE")

    def test_another_users_project_is_not_found(self):
        self.make_version(self.client, self.project)
        self.client_for_other(self.make_user("sub-stranger"))
        self.assertEqual(self.submit(self.client, self.pid).status_code, 404)
        self.assertEqual(self.client.get(f"/mlflow/projects/{self.pid}/runs").status_code, 404)

    def test_oversized_hyperparameters_are_refused(self):
        self.make_version(self.client, self.project)
        response = self.submit(self.client, self.pid, hyperparameters={"blob": "x" * 70000})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.jobs(), [])

    def test_submission_triggers_a_scheduling_pass_only_when_a_job_was_created(self):
        self.make_version(self.client, self.project)
        self.submit(self.client, self.pid, client_request_id="p-1")
        self.submit(self.client, self.pid, client_request_id="p-1")
        self.assertEqual(len(self.passes), 1)


@requires_postgres
class RunDatasetTests(GpuRunCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("data")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.pid = self.project["project_id"]
        self.make_version(self.client, self.project)
        self.rows = b"x,y\n1,2\n3,4\n"
        self.labels = b"0\n1\n"

    def test_a_dataset_that_is_not_staged_is_refused_naming_the_files(self):
        response = self.submit(self.client, self.pid, dataset=[self.dataset_entry("data/train.csv", self.rows)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "DATASET_NOT_STAGED")
        self.assertIn("data/train.csv", response.json()["detail"])
        self.assertEqual(self.db.query(orm.Job).filter_by(user_id=self.user.id).count(), 0)

    def test_a_staged_dataset_is_fingerprinted_for_the_run(self):
        self.assertEqual(self.stage(self.client, self.rows).status_code, 201)
        self.assertEqual(self.stage(self.client, self.labels).status_code, 201)
        entries = [self.dataset_entry("data/train.csv", self.rows), self.dataset_entry("labels.txt", self.labels)]
        response = self.submit(self.client, self.pid, dataset=entries)
        self.assertEqual(response.status_code, 201, response.text)
        shown = {f["path"]: f for f in response.json()["dataset"]}
        self.assertEqual(shown["data/train.csv"], {**entries[0], "staged": True})
        self.assertEqual(self.db.query(RunDatasetFile).filter_by(run_id=response.json()["run_id"]).count(), 2)

    def test_a_declared_size_that_differs_from_the_staged_file_is_refused(self):
        self.stage(self.client, self.rows)
        entry = {**self.dataset_entry("data/train.csv", self.rows), "size_bytes": len(self.rows) + 1}
        self.assertEqual(self.submit(self.client, self.pid, dataset=[entry]).status_code, 409)

    def test_a_run_may_have_no_dataset(self):
        response = self.submit(self.client, self.pid)
        self.assertEqual((response.status_code, response.json()["dataset"]), (201, []))

    def test_unsafe_or_duplicate_dataset_paths_are_refused(self):
        self.stage(self.client, self.rows)
        for path in ("../escape.csv", "/etc/passwd", "a//b.csv", "dir/./x.csv", "back\\slash.csv", "c:/x.csv"):
            with self.subTest(path=path):
                response = self.submit(self.client, self.pid, dataset=[self.dataset_entry(path, self.rows)])
                self.assertEqual(response.status_code, 400, response.text)
        twice = [self.dataset_entry("same.csv", self.rows)] * 2
        self.assertEqual(self.submit(self.client, self.pid, dataset=twice).status_code, 400)
        self.assertEqual(self.db.query(orm.Job).filter_by(user_id=self.user.id).count(), 0)

    def test_a_malformed_digest_is_a_validation_error(self):
        entry = {"path": "x.csv", "size_bytes": 1, "blake3": "nothex"}
        self.assertEqual(self.submit(self.client, self.pid, dataset=[entry]).status_code, 422)

    def test_two_runs_can_share_one_staged_file(self):
        self.stage(self.client, self.rows)
        entries = [self.dataset_entry("data/train.csv", self.rows)]
        first = self.submit(self.client, self.pid, dataset=entries).json()
        second = self.submit(self.client, self.pid, dataset=entries).json()
        self.assertEqual((first["run_number"], second["run_number"]), (1, 2))
        shared = self.db.query(RunDatasetFile).filter(RunDatasetFile.run_id.in_([first["run_id"], second["run_id"]]))
        self.assertEqual(len({row.staged_file_id for row in shared}), 1)
        self.assertEqual(shared.count(), 2)


@requires_postgres
class RunReadingTests(GpuRunCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("read")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.pid = self.project["project_id"]

    def test_runs_list_newest_first_and_filter_by_version(self):
        self.make_version(self.client, self.project, {"main.py": b"v1\n"})
        self.submit(self.client, self.pid)
        self.make_version(self.client, self.project, {"main.py": b"v2\n"})
        self.submit(self.client, self.pid)
        self.submit(self.client, self.pid)
        everything = self.client.get(f"/mlflow/projects/{self.pid}/runs").json()
        self.assertEqual([r["label"] for r in everything], ["B run 2", "B run 1", "A run 1"])
        only_a = self.client.get(f"/mlflow/projects/{self.pid}/runs", params={"version": "A"}).json()
        self.assertEqual([r["label"] for r in only_a], ["A run 1"])

    def test_one_run_is_read_by_version_and_number(self):
        self.make_version(self.client, self.project)
        self.submit(self.client, self.pid)
        shown = self.client.get(f"/mlflow/projects/{self.pid}/runs/A/1")
        self.assertEqual((shown.status_code, shown.json()["label"]), (200, "A run 1"))
        self.assertEqual(self.client.get(f"/mlflow/projects/{self.pid}/runs/A/9").status_code, 404)
        self.assertEqual(self.client.get(f"/mlflow/projects/{self.pid}/runs/Q/1").status_code, 404)

    def test_the_versions_list_counts_runs(self):
        self.make_version(self.client, self.project)
        self.submit(self.client, self.pid)
        self.submit(self.client, self.pid)
        self.assertEqual(self.client.get(f"/mlflow/projects/{self.pid}/versions").json()[0]["run_count"], 2)

    def test_status_summarises_the_project(self):
        empty = self.client.get(f"/mlflow/projects/{self.pid}/status").json()
        self.assertEqual((empty["current_version"], empty["head_version"], empty["runs"], empty["latest_run"]),
                         (None, None, 0, None))
        self.make_version(self.client, self.project, {"main.py": b"v1\n"})
        self.make_version(self.client, self.project, {"main.py": b"v2\n"})
        self.client.put(f"/mlflow/projects/{self.pid}/current", json={"version": "A"})
        self.submit(self.client, self.pid)
        shown = self.client.get(f"/mlflow/projects/{self.pid}/status").json()
        self.assertEqual((shown["current_version"], shown["head_version"], shown["versions"], shown["runs"]),
                         ("A", "B", 2, 1))
        self.assertEqual(shown["latest_run"]["label"], "A run 1")


if __name__ == "__main__":
    unittest.main()
