import os
import unittest
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

from logic import staging
from mlflow_models import ModelRun, RunDatasetFile, StagedFile
from tests.gpu_case import GpuRunCase, requires_postgres


@requires_postgres
class StagingUploadTests(GpuRunCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("stg")
        self.client = self.client_as(self.user)
        self.payload = f"id,value\n{uuid4().hex}\n".encode()
        self.digest = self.blake3_of(self.payload)

    def staged_rows(self, user=None):
        self.db.expire_all()
        return self.db.query(StagedFile).filter_by(user_id=(user or self.user).id).all()

    def check(self, client, files):
        return client.post("/mlflow/staging/missing", json={"files": files}).json()["missing"]

    def test_an_upload_is_staged_and_reported_created(self):
        response = self.stage(self.client, self.payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"blake3": self.digest, "size_bytes": len(self.payload), "created": True})
        self.assertEqual((self.storage / self.digest).read_bytes(), self.payload)

    def test_uploading_the_same_file_again_is_a_no_op(self):
        self.stage(self.client, self.payload)
        again = self.stage(self.client, self.payload)
        self.assertEqual((again.status_code, again.json()["created"]), (200, False))
        self.assertEqual(len(self.staged_rows()), 1)

    def test_missing_lists_only_what_is_not_staged(self):
        self.stage(self.client, self.payload)
        other = self.blake3_of(b"something else")
        files = [{"blake3": self.digest, "size_bytes": len(self.payload)}, {"blake3": other, "size_bytes": 14}]
        self.assertEqual(self.check(self.client, files), [other])

    def test_a_file_with_the_wrong_size_counts_as_missing(self):
        self.stage(self.client, self.payload)
        self.assertEqual(self.check(self.client, [{"blake3": self.digest, "size_bytes": 1}]), [self.digest])

    def test_bytes_that_do_not_match_the_digest_are_refused_and_nothing_is_staged(self):
        response = self.stage(self.client, b"x" * len(self.payload), digest=self.digest, size=len(self.payload))
        self.assertEqual(response.status_code, 400)
        self.assertIn("BLAKE3", response.json()["detail"])
        self.assertEqual(self.staged_rows(), [])
        self.assertFalse((self.storage / self.digest).exists())

    def test_a_short_or_long_body_is_refused(self):
        self.assertEqual(self.stage(self.client, self.payload, size=len(self.payload) + 5).status_code, 400)
        self.assertEqual(self.stage(self.client, self.payload, size=len(self.payload) - 5).status_code, 400)
        self.assertEqual(self.staged_rows(), [])

    def test_a_malformed_digest_or_negative_size_is_refused(self):
        self.assertEqual(self.stage(self.client, self.payload, digest="abc").status_code, 400)
        self.assertEqual(self.stage(self.client, self.payload, size=-1).status_code, 400)

    def test_a_file_over_the_size_limit_is_refused_before_it_is_read(self):
        with patch.dict(os.environ, {"PRAYOG_STAGING_MAX_FILE_BYTES": "10"}):
            response = self.stage(self.client, self.payload)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.staged_rows(), [])

    def test_no_temporary_files_are_left_behind_after_a_refusal(self):
        self.stage(self.client, b"wrong", digest=self.digest, size=5)
        self.assertEqual([p.name for p in self.storage.iterdir() if p.name.startswith(".")], [])

    def test_staging_is_private_to_each_user(self):
        self.stage(self.client, self.payload)
        stranger = self.make_user("stg-other")
        other = self.client_for_other(stranger)
        files = [{"blake3": self.digest, "size_bytes": len(self.payload)}]
        self.assertEqual(self.check(other, files), [self.digest])          # not told the other user holds it
        self.assertEqual(self.stage(other, self.payload).json()["created"], True)
        self.assertEqual((len(self.staged_rows(self.user)), len(self.staged_rows(stranger))), (1, 1))

    def test_an_empty_file_can_be_staged(self):
        empty = b""
        self.assertEqual(self.stage(self.client, empty).status_code, 201)


@requires_postgres
class StagingExpiryTests(GpuRunCase):
    """Staged data is deleted a fixed time after the last run that used it ends; fingerprints stay."""

    def setUp(self):
        super().setUp()
        self.user = self.make_user("exp")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.pid = self.project["project_id"]
        self.make_version(self.client, self.project)
        self.payload = f"a,b\n{uuid4().hex}\n".encode()
        self.digest = self.blake3_of(self.payload)
        self.hold = timedelta(hours=48)

    def staged(self, digest=None, user=None) -> StagedFile:
        self.db.expire_all()
        return self.db.query(StagedFile).filter_by(user_id=(user or self.user).id, blake3=digest or self.digest).one()

    def run_with_data(self, payload=None) -> dict:
        payload = payload or self.payload
        self.stage(self.client, payload)
        response = self.submit(self.client, self.pid, dataset=[self.dataset_entry("d.csv", payload)])
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def finish(self, run_id: int, status="SUCCEEDED", ended_ago=timedelta(0)):
        run = self.db.get(ModelRun, run_id)
        run.status = status
        run.finished_at = self.now - ended_ago
        for staged in self.db.query(StagedFile).join(RunDatasetFile, RunDatasetFile.staged_file_id == StagedFile.id) \
                .filter(RunDatasetFile.run_id == run_id):
            staged.created_at = min(staged.created_at, run.finished_at - timedelta(hours=1))    # uploaded before it ended
        self.db.commit()

    def expire(self, **kw):
        return staging.expire_staged_files(self.db, now=self.now, hold=self.hold, **kw)

    def test_data_is_kept_while_its_run_is_unfinished_however_old(self):
        self.run_with_data()
        staged = self.staged()
        staged.created_at = self.now - timedelta(days=30)
        self.db.commit()
        self.assertEqual(self.expire(), 0)
        self.assertIsNone(self.staged().deleted_at)
        self.assertTrue((self.storage / self.digest).exists())

    def test_data_is_kept_for_the_hold_after_the_run_ends(self):
        run = self.run_with_data()
        self.finish(run["run_id"], ended_ago=timedelta(hours=47))
        self.assertEqual(self.expire(), 0)
        self.assertTrue((self.storage / self.digest).exists())

    def test_data_is_deleted_once_the_hold_has_passed(self):
        run = self.run_with_data()
        self.finish(run["run_id"], ended_ago=timedelta(hours=49))
        self.assertEqual(self.expire(), 1)
        self.assertIsNotNone(self.staged().deleted_at)
        self.assertFalse((self.storage / self.digest).exists())

    def test_a_failed_or_cancelled_run_starts_the_clock_too(self):
        for status in ("FAILED", "CANCELLED"):
            with self.subTest(status=status):
                payload = f"{status}-{uuid4().hex}\n".encode()
                run = self.run_with_data(payload)
                self.finish(run["run_id"], status=status, ended_ago=timedelta(hours=60))
                self.assertEqual(self.expire(), 1)
                self.assertIsNotNone(self.staged(self.blake3_of(payload)).deleted_at)

    def test_the_fingerprint_survives_deletion_and_shows_the_data_gone(self):
        run = self.run_with_data()
        self.finish(run["run_id"], ended_ago=timedelta(hours=49))
        self.expire()
        shown = self.client.get(f"/mlflow/projects/{self.pid}/runs/A/{run['run_number']}").json()
        self.assertEqual(shown["dataset"], [{**self.dataset_entry("d.csv", self.payload), "staged": False}])

    def test_a_later_run_using_the_data_extends_its_life(self):
        first = self.run_with_data()
        self.finish(first["run_id"], ended_ago=timedelta(hours=60))
        second = self.submit(self.client, self.pid, dataset=[self.dataset_entry("d.csv", self.payload)]).json()
        self.assertEqual(self.expire(), 0)                                    # the rerun is queued: data in use
        self.finish(second["run_id"], ended_ago=timedelta(hours=1))
        self.assertEqual(self.expire(), 0)                                    # and held from when it ended
        self.assertIsNone(self.staged().deleted_at)

    def test_data_nobody_used_is_deleted_after_the_hold_from_upload(self):
        self.stage(self.client, self.payload)
        staged = self.staged()
        staged.created_at = self.now - timedelta(hours=10)
        self.db.commit()
        self.assertEqual(self.expire(), 0)
        staged.created_at = self.now - timedelta(hours=50)
        self.db.commit()
        self.assertEqual(self.expire(), 1)

    def test_expiry_is_idempotent(self):
        run = self.run_with_data()
        self.finish(run["run_id"], ended_ago=timedelta(hours=49))
        self.assertEqual((self.expire(), self.expire()), (1, 0))

    def test_deleted_data_can_be_staged_again_and_used(self):
        run = self.run_with_data()
        self.finish(run["run_id"], ended_ago=timedelta(hours=49))
        self.expire()
        self.assertEqual(self.stage(self.client, self.payload).json()["created"], True)
        self.assertIsNone(self.staged().deleted_at)
        self.assertTrue((self.storage / self.digest).exists())
        self.assertEqual(self.expire(), 0)                                    # a fresh clock

    def test_the_hold_comes_from_the_environment(self):
        for value, expected in (("12", timedelta(hours=12)), ("junk", timedelta(hours=48)), ("-5", timedelta(0))):
            with self.subTest(value=value), patch.dict(os.environ, {"PRAYOG_STAGING_RETENTION_HOURS": value}):
                self.assertEqual(staging.retention(), expected)

    def test_the_object_is_kept_while_an_artifact_names_the_same_content(self):
        artifact = self.upload(self.client, self.project["experiment_id"], "CODE", self.payload)
        self.assertEqual(artifact["id"] is not None, True)
        run = self.run_with_data()
        self.finish(run["run_id"], ended_ago=timedelta(hours=49))
        self.assertEqual(self.expire(), 1)
        self.assertIsNotNone(self.staged().deleted_at)
        self.assertTrue((self.storage / self.digest).exists())                # the artifact still needs it

    def test_the_object_is_kept_while_another_user_still_has_it_staged(self):
        run = self.run_with_data()
        stranger = self.make_user("exp-other")
        self.stage(self.client_for_other(stranger), self.payload)
        self.client_for_other(self.user)
        self.finish(run["run_id"], ended_ago=timedelta(hours=49))
        self.assertEqual(self.expire(), 1)                                    # my copy goes (theirs is unused but young)
        self.assertIsNotNone(self.staged().deleted_at)
        self.assertIsNone(self.staged(user=stranger).deleted_at)
        self.assertTrue((self.storage / self.digest).exists())

    def test_clearing_a_project_deletes_finished_data_but_keeps_data_in_use(self):
        finished, running = f"done {uuid4().hex}\n".encode(), f"busy {uuid4().hex}\n".encode()
        done = self.run_with_data(finished)
        self.finish(done["run_id"], ended_ago=timedelta(minutes=5))
        self.run_with_data(running)
        result = self.client.delete(f"/mlflow/projects/{self.pid}/staged-data").json()
        self.assertEqual((result["deleted_files"], result["kept_in_use"], result["freed_bytes"]),
                         (1, 1, len(finished)))
        self.assertIsNotNone(self.staged(self.blake3_of(finished)).deleted_at)
        self.assertIsNone(self.staged(self.blake3_of(running)).deleted_at)

    def test_clearing_does_not_touch_another_projects_data(self):
        run = self.run_with_data()
        self.finish(run["run_id"])
        other = self.make_project(self.client)
        self.assertEqual(self.client.delete(f"/mlflow/projects/{other['project_id']}/staged-data").json()["deleted_files"], 0)
        self.assertIsNone(self.staged().deleted_at)

    def test_clearing_someone_elses_project_is_not_found(self):
        self.client_for_other(self.make_user("exp-stranger"))
        self.assertEqual(self.client.delete(f"/mlflow/projects/{self.pid}/staged-data").status_code, 404)

    def test_status_reports_the_data_still_held(self):
        self.run_with_data()
        shown = self.client.get(f"/mlflow/projects/{self.pid}/status").json()
        self.assertEqual((shown["staged_files"], shown["staged_bytes"]), (1, len(self.payload)))


if __name__ == "__main__":
    unittest.main()
