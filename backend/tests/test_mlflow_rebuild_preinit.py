import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from logic.errors import BadRequestError
from logic.mlflow import create_rebuild


class RebuildPreinitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=7)

    def set_snapshot_query_result(self, snapshot):
        self.db.query.return_value.filter.return_value.first.return_value = snapshot

    def test_missing_snapshot_is_generic_preinit_rejection_without_attempt(self):
        self.set_snapshot_query_result(None)

        with self.assertRaises(BadRequestError) as raised:
            create_rebuild(self.db, "missing-snapshot", self.user, {})

        self.assertEqual(str(raised.exception), "REBUILD_REJECTED: snapshot could not be validated")
        self.db.add.assert_not_called()
        self.db.flush.assert_not_called()
        self.db.commit.assert_not_called()

    def test_non_recorded_snapshot_has_same_response_as_missing_snapshot(self):
        self.set_snapshot_query_result(SimpleNamespace(state="INCOMPLETE"))

        with self.assertRaises(BadRequestError) as raised:
            create_rebuild(self.db, "incomplete-snapshot", self.user, {})

        self.assertEqual(str(raised.exception), "REBUILD_REJECTED: snapshot could not be validated")
        self.db.add.assert_not_called()
        self.db.flush.assert_not_called()
        self.db.commit.assert_not_called()

    @patch("logic.mlflow.get_verified_artifact", side_effect=OSError("private storage path detail"))
    def test_artifact_verification_failure_does_not_leak_details_or_create_attempt(self, verify):
        snapshot = SimpleNamespace(
            state="RECORDED",
            code_artifact_id="artifact-code",
            code_sha256="a" * 64,
            experiment_id=3,
            snapshot_id="snapshot-1",
        )
        self.set_snapshot_query_result(snapshot)

        with self.assertRaises(BadRequestError) as raised:
            create_rebuild(self.db, "snapshot-1", self.user, {})

        self.assertEqual(str(raised.exception), "REBUILD_REJECTED: snapshot could not be validated")
        self.assertNotIn("private storage path detail", str(raised.exception))
        verify.assert_called_once()
        self.db.add.assert_not_called()
        self.db.flush.assert_not_called()
        self.db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
