import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from logic.errors import ConflictError, ForbiddenError, NotFoundError
from logic.mlflow_handover import (
    approve_handover,
    cancel_handover,
    push_project,
    reject_handover,
)
from routers.handover import router


class ProjectHandoverTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.owner = SimpleNamespace(id=10, role="STUDENT")
        self.professor = SimpleNamespace(id=20, role="PROFESSOR")
        self.profile = SimpleNamespace(id=4, user_id=20)
        self.pending = SimpleNamespace(
            handover_id="handover-1",
            project_id="project-1",
            source_owner_id=10,
            target_professor_id=4,
            status="HANDOVER_PENDING",
            artifact_inventory=[],
            failed_artifacts=[],
            created_at=datetime(2026, 1, 1),
            decided_at=None,
        )

    def test_push_rejects_unassigned_professor_before_creating_request(self):
        with patch("logic.mlflow_handover._project_for_owner", return_value=SimpleNamespace(project_id="project-1")), patch(
            "logic.mlflow_handover._target_professor", side_effect=ForbiddenError("not supervisor")
        ):
            with self.assertRaises(ForbiddenError):
                push_project(self.db, "project-1", self.owner, 99)
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_rejection_is_terminal_and_committed(self):
        with patch("logic.mlflow_handover._handover_for_decision", return_value=(self.pending, self.profile)):
            result = reject_handover(self.db, "handover-1", self.professor)
        self.assertIs(result, self.pending)
        self.assertEqual(result.status, "HANDOVER_REJECTED")
        self.assertIsNotNone(result.decided_at)
        self.db.commit.assert_called_once()

    def test_rejected_handover_cannot_be_approved(self):
        self.pending.status = "HANDOVER_REJECTED"
        with patch("logic.mlflow_handover._handover_for_decision", return_value=(self.pending, self.profile)):
            with self.assertRaises(ConflictError):
                approve_handover(self.db, "handover-1", self.professor)
        self.db.commit.assert_not_called()

    def test_cancelled_handover_cannot_be_approved(self):
        self.pending.status = "HANDOVER_CANCELLED"
        with patch("logic.mlflow_handover._handover_for_decision", return_value=(self.pending, self.profile)):
            with self.assertRaises(ConflictError):
                approve_handover(self.db, "handover-1", self.professor)
        self.db.commit.assert_not_called()

    def test_handover_endpoints_are_registered(self):
        paths = {route.path for route in router.routes}
        self.assertIn("/mlflow/projects/{project_id}/handover", paths)
        self.assertIn("/mlflow/handovers/{handover_id}/approve", paths)
        self.assertIn("/mlflow/handovers/{handover_id}/reject", paths)
        self.assertIn("/mlflow/handovers/{handover_id}/cancel", paths)



    def test_pending_handover_cannot_be_rejected_twice(self):
        self.pending.status = "HANDOVER_REJECTED"
        with patch("logic.mlflow_handover._handover_for_decision", return_value=(self.pending, self.profile)):
            with self.assertRaisesRegex(ConflictError, "HANDOVER_PENDING"):
                reject_handover(self.db, "handover-1", self.professor)
        self.db.commit.assert_not_called()

    def test_approved_handover_cannot_be_rejected(self):
        self.pending.status = "HANDOVER_APPROVED"
        with patch("logic.mlflow_handover._handover_for_decision", return_value=(self.pending, self.profile)):
            with self.assertRaises(ConflictError):
                reject_handover(self.db, "handover-1", self.professor)
        self.db.commit.assert_not_called()

    def test_cancel_handover_marks_pending_request_terminal(self):
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = self.pending

        result = cancel_handover(self.db, "handover-1", self.owner)

        self.assertIs(result, self.pending)
        self.assertEqual(result.status, "HANDOVER_CANCELLED")
        self.assertIsNotNone(result.decided_at)
        self.db.commit.assert_called_once()

    def test_cancel_cannot_be_repeated_after_terminal_transition(self):
        self.pending.status = "HANDOVER_CANCELLED"
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = self.pending

        with self.assertRaisesRegex(ConflictError, "HANDOVER_PENDING"):
            cancel_handover(self.db, "handover-1", self.owner)

        self.db.commit.assert_not_called()

    def test_cancel_rejects_request_owned_by_another_user(self):
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = None

        with self.assertRaises(NotFoundError):
            cancel_handover(self.db, "handover-1", SimpleNamespace(id=999, role="STUDENT"))

        self.db.commit.assert_not_called()

    def test_handover_visibility_is_limited_to_owner_target_professor_and_admin(self):
        with patch("logic.mlflow_handover._handover_for_decision", return_value=(self.pending, self.profile)):
            self.assertIs(get_handover(self.db, "handover-1", self.owner), self.pending)

        self.db.reset_mock()
        self.db.query.return_value.filter.return_value.first.return_value = self.profile
        self.assertIs(get_handover(self.db, "handover-1", self.professor), self.pending)

        self.db.reset_mock()
        self.db.query.return_value.filter.return_value.first.return_value = self.pending
        admin = SimpleNamespace(id=30, role="ADMIN")
        self.assertIs(get_handover(self.db, "handover-1", admin), self.pending)

    def test_unrelated_user_cannot_view_handover(self):
        from logic.errors import ForbiddenError
        handover_query = MagicMock()
        handover_query.filter.return_value.first.return_value = self.pending
        professor_query = MagicMock()
        professor_query.filter.return_value.first.return_value = None
        self.db.query.side_effect = [handover_query, professor_query]

        with self.assertRaises(ForbiddenError):
            get_handover(self.db, "handover-1", SimpleNamespace(id=999, role="STUDENT"))

    def test_inventory_verification_detects_added_removed_and_modified_artifacts(self):
        expected = [
            {"artifact_id": "same", "kind": "CODE", "sha256": "a" * 64, "size_bytes": 10},
            {"artifact_id": "removed", "kind": "LOG", "sha256": "b" * 64, "size_bytes": 20},
            {"artifact_id": "changed", "kind": "WEIGHTS", "sha256": "c" * 64, "size_bytes": 30},
        ]
        handover = SimpleNamespace(
            artifact_inventory=expected, project_id="project-1", source_owner_id=10
        )
        same = SimpleNamespace(id="same", kind="CODE", sha256="a" * 64, size_bytes=10, experiment_id=1)
        changed = SimpleNamespace(id="changed", kind="WEIGHTS", sha256="x" * 64, size_bytes=30, experiment_id=1)
        added = SimpleNamespace(id="added", kind="LOG", sha256="d" * 64, size_bytes=40, experiment_id=1)
        query = self.db.query.return_value.filter.return_value
        query.order_by.return_value.all.return_value = [same, changed, added]

        with patch("logic.mlflow_handover.get_verified_artifact", return_value=(same, Path("/artifact"))):
            failed = _verify_inventory(self.db, handover)

        self.assertEqual(failed, ["added", "changed", "removed"])

    def test_inventory_verification_marks_artifact_corruption_as_failure(self):
        entry = {"artifact_id": "artifact-1", "kind": "CODE", "sha256": "a" * 64, "size_bytes": 10}
        artifact = SimpleNamespace(
            id="artifact-1", kind="CODE", sha256="a" * 64, size_bytes=10, experiment_id=1
        )
        handover = SimpleNamespace(
            artifact_inventory=[entry], project_id="project-1", source_owner_id=10
        )
        query = self.db.query.return_value.filter.return_value
        query.order_by.return_value.all.return_value = [artifact]

        with patch("logic.mlflow_handover.get_verified_artifact", side_effect=OSError("missing")):
            self.assertEqual(_verify_inventory(self.db, handover), ["artifact-1"])


    def test_push_rejects_second_pending_handover_for_same_project(self):
        existing = SimpleNamespace(status="HANDOVER_PENDING")
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = existing

        with patch("logic.mlflow_handover._project_for_owner", return_value=SimpleNamespace(project_id="project-1")), patch(
            "logic.mlflow_handover._target_professor", return_value=self.profile
        ):
            with self.assertRaisesRegex(ConflictError, "already pending"):
                push_project(self.db, "project-1", self.owner, 20)

        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_push_rejects_project_without_owned_artifacts(self):
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = None
        query.order_by.return_value.all.return_value = []

        with patch("logic.mlflow_handover._project_for_owner", return_value=SimpleNamespace(project_id="project-1")), patch(
            "logic.mlflow_handover._target_professor", return_value=self.profile
        ):
            from logic.errors import BadRequestError
            with self.assertRaisesRegex(BadRequestError, "no owned artifacts"):
                push_project(self.db, "project-1", self.owner, 20)

        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

if __name__ == "__main__":
    unittest.main()
