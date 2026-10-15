import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from logic.errors import ConflictError, ForbiddenError
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


if __name__ == "__main__":
    unittest.main()
