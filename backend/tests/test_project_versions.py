import unittest

from logic.errors import BadRequestError
from logic.project_versions import parse_version, version_label
from tests.gpu_case import GpuRunCase, requires_postgres


class VersionLabelTests(unittest.TestCase):
    def test_labels_count_like_spreadsheet_columns(self):
        self.assertEqual([version_label(n) for n in (1, 2, 26, 27, 28, 52, 53, 702, 703)],
                         ["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"])

    def test_labels_round_trip(self):
        for n in range(1, 1500):
            self.assertEqual(parse_version(version_label(n)), n)

    def test_parse_accepts_lowercase_and_numbers(self):
        self.assertEqual((parse_version("b"), parse_version("3"), parse_version(" aa ")), (2, 3, 27))

    def test_parse_rejects_nonsense(self):
        for bad in ("", "  ", "0", "-1", "A1", "v2", "B C", "é"):
            with self.subTest(bad=bad), self.assertRaises(BadRequestError):
                parse_version(bad)

    def test_zero_has_no_label(self):
        with self.assertRaises(ValueError):
            version_label(0)


@requires_postgres
class ProjectVersionApiTests(GpuRunCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("vers")
        self.client = self.client_as(self.user)
        self.project = self.make_project(self.client)
        self.pid = self.project["project_id"]

    def versions(self):
        return self.client.get(f"/mlflow/projects/{self.pid}/versions").json()

    def test_first_upload_is_version_a_and_current(self):
        version = self.make_version(self.client, self.project)
        self.assertEqual((version["version"], version["created"], version["is_head"], version["is_current"]),
                         ("A", True, True, True))
        self.assertEqual(len(version["manifest_hash"]), 64)
        self.assertEqual(version["run_count"], 0)

    def test_manifest_hash_is_the_blake3_storage_key_of_the_manifest(self):
        from mlflow_models import Artifact
        version = self.make_version(self.client, self.project)
        stored = self.db.get(Artifact, version["manifest_artifact_id"])
        self.assertEqual(version["manifest_hash"], stored.storage_key)

    def test_registering_the_same_content_again_creates_nothing(self):
        files = {"main.py": b"print(1)\n"}
        first = self.make_version(self.client, self.project, files)
        again = self.make_version(self.client, self.project, files)
        self.assertEqual((again["version"], again["created"]), ("A", False))
        self.assertEqual(again["manifest_hash"], first["manifest_hash"])
        self.assertEqual(len(self.versions()), 1)

    def test_changed_content_is_the_next_version_and_moves_head_and_current(self):
        self.make_version(self.client, self.project, {"main.py": b"v1\n"})
        second = self.make_version(self.client, self.project, {"main.py": b"v2\n"})
        self.assertEqual((second["version"], second["created"]), ("B", True))
        flags = {v["version"]: (v["is_head"], v["is_current"]) for v in self.versions()}
        self.assertEqual(flags, {"B": (True, True), "A": (False, False)})

    def test_versions_list_newest_first(self):
        for n in range(3):
            self.make_version(self.client, self.project, {"main.py": f"v{n}\n".encode()})
        self.assertEqual([v["version"] for v in self.versions()], ["C", "B", "A"])

    def test_checkout_moves_current_but_not_head(self):
        self.make_version(self.client, self.project, {"main.py": b"v1\n"})
        self.make_version(self.client, self.project, {"main.py": b"v2\n"})
        response = self.client.put(f"/mlflow/projects/{self.pid}/current", json={"version": "a"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["version"], response.json()["is_current"], response.json()["is_head"]),
                         ("A", True, False))
        flags = {v["version"]: (v["is_head"], v["is_current"]) for v in self.versions()}
        self.assertEqual(flags, {"B": (True, False), "A": (False, True)})

    def test_registering_an_older_content_makes_it_current_again(self):
        self.make_version(self.client, self.project, {"main.py": b"v1\n"})
        self.make_version(self.client, self.project, {"main.py": b"v2\n"})
        back = self.make_version(self.client, self.project, {"main.py": b"v1\n"})     # the folder was reverted
        self.assertEqual((back["version"], back["created"], back["is_current"], back["is_head"]),
                         ("A", False, True, False))

    def test_checkout_of_a_missing_version_is_not_found(self):
        self.make_version(self.client, self.project)
        response = self.client.put(f"/mlflow/projects/{self.pid}/current", json={"version": "C"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("no version C", response.json()["detail"])

    def test_checkout_of_a_malformed_version_is_a_bad_request(self):
        self.make_version(self.client, self.project)
        self.assertEqual(self.client.put(f"/mlflow/projects/{self.pid}/current", json={"version": "1x"}).status_code, 400)

    def test_a_manifest_naming_a_foreign_chunk_is_refused(self):
        other = self.make_user("vers-other")
        other_client = self.client_for_other(other)
        other_project = self.make_project(other_client)
        foreign_manifest = self.make_manifest(other_client, other_project, {"main.py": b"theirs\n"})
        self.client_for_other(self.user)
        response = self.client.post(f"/mlflow/projects/{self.pid}/versions",
                                    json={"manifest_artifact_id": foreign_manifest})
        self.assertEqual(response.status_code, 404)

    def test_an_artifact_that_is_not_a_manifest_is_refused(self):
        junk = self.upload(self.client, self.project["experiment_id"], "CODE", b"just some bytes")
        response = self.client.post(f"/mlflow/projects/{self.pid}/versions", json={"manifest_artifact_id": junk["id"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.versions(), [])

    def test_a_weights_artifact_cannot_be_a_version(self):
        weights = self.upload(self.client, self.project["experiment_id"], "WEIGHTS", b"w")
        self.assertEqual(self.client.post(f"/mlflow/projects/{self.pid}/versions",
                                          json={"manifest_artifact_id": weights["id"]}).status_code, 404)

    def test_a_manifest_whose_chunk_record_disagrees_is_refused(self):
        import json
        chunk = self.upload(self.client, self.project["experiment_id"], "CODE", b"a chunk")
        manifest = {"format_version": 1, "experiment_id": self.project["experiment_id"],
                    "files": [{"path": "main.py"}],
                    "chunks": [{"index": 0, "artifact_id": chunk["id"], "sha256": "0" * 64, "size_bytes": 7}]}
        stored = self.upload(self.client, self.project["experiment_id"], "CODE", json.dumps(manifest).encode())
        response = self.client.post(f"/mlflow/projects/{self.pid}/versions", json={"manifest_artifact_id": stored["id"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("code chunk", response.json()["detail"])

    def test_another_user_cannot_see_or_change_the_project_versions(self):
        self.make_version(self.client, self.project)
        stranger = self.make_user("vers-stranger")
        self.client_for_other(stranger)
        self.assertEqual(self.client.get(f"/mlflow/projects/{self.pid}/versions").status_code, 404)
        self.assertEqual(self.client.put(f"/mlflow/projects/{self.pid}/current", json={"version": "A"}).status_code, 404)

    def test_version_numbers_stay_unique_under_repeated_registration(self):
        from mlflow_models import ProjectVersion
        for n in range(4):
            self.make_version(self.client, self.project, {"main.py": f"v{n}\n".encode()})
        numbers = [v.version_number for v in self.db.query(ProjectVersion).filter_by(project_id=self.pid)]
        self.assertEqual(sorted(numbers), [1, 2, 3, 4])

    def test_each_version_points_at_its_parent(self):
        from mlflow_models import ProjectVersion
        for n in range(3):
            self.make_version(self.client, self.project, {"main.py": f"v{n}\n".encode()})
        rows = {v.version_number: v for v in self.db.query(ProjectVersion).filter_by(project_id=self.pid)}
        self.assertEqual((rows[1].parent_version_id, rows[2].parent_version_id, rows[3].parent_version_id),
                         (None, rows[1].id, rows[2].id))


if __name__ == "__main__":
    unittest.main()
