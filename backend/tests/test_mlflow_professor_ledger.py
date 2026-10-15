import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from logic.errors import ForbiddenError
from logic.mlflow_professor_ledger import list_professor_ledger


class ProfessorLedgerTests(unittest.TestCase):
    def test_only_professor_can_read_ledger(self):
        with self.assertRaises(ForbiddenError):
            list_professor_ledger(MagicMock(), SimpleNamespace(id=9, role="STUDENT"))

    def test_returns_only_enrolled_students_recorded_snapshots_as_metadata(self):
        db = MagicMock()
        professor = SimpleNamespace(id=4, user_id=9)
        student = SimpleNamespace(user_id=12, name="Student One", roll_number="123456")
        run = SimpleNamespace(id=30, user_id=12)
        snapshot = SimpleNamespace(
            run_id=30, user_id=12, snapshot_id="snapshot-30", experiment_id=5,
            project_id="project-1", model_id="model-2", state="RECORDED",
            code_sha256="a" * 64,
        )
        entry = SimpleNamespace(
            payload={
                "run_id": 30, "snapshot_id": "snapshot-30", "state": "RECORDED",
                "experiment_id": 5, "project_id": "project-1", "model_id": "model-2",
                "code_sha256": "a" * 64,
            },
            consumed_at=datetime(2026, 10, 15, 4, 0, 0),
        )
        professor_query = MagicMock()
        professor_query.filter.return_value.first.return_value = professor
        student_query = MagicMock()
        student_query.filter.return_value.all.return_value = [student]
        run_query = MagicMock()
        run_query.filter.return_value.all.return_value = [run]
        entry_query = MagicMock()
        entry_query.filter.return_value.order_by.return_value.all.return_value = [entry]
        snapshot_query = MagicMock()
        snapshot_query.filter.return_value.all.return_value = [snapshot]
        db.query.side_effect = [professor_query, student_query, run_query, entry_query, snapshot_query]

        result = list_professor_ledger(db, SimpleNamespace(id=9, role="PROFESSOR"))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["student_name"], "Student One")
        self.assertEqual(result[0]["student_roll_number"], "123456")
        self.assertEqual(result[0]["snapshot_id"], "snapshot-30")
        self.assertEqual(result[0]["state"], "RECORDED")
        self.assertNotIn("hyperparameters", result[0])
        self.assertNotIn("steward", result[0])
        self.assertNotIn("rag", result[0])

    def test_skips_ledger_rows_without_matching_persisted_snapshot(self):
        db = MagicMock()
        professor_query = MagicMock()
        professor_query.filter.return_value.first.return_value = SimpleNamespace(id=4)
        student_query = MagicMock()
        student_query.filter.return_value.all.return_value = [
            SimpleNamespace(user_id=12, name="Student One", roll_number="123456")
        ]
        run_query = MagicMock()
        run_query.filter.return_value.all.return_value = [SimpleNamespace(id=30, user_id=12)]
        entry_query = MagicMock()
        entry_query.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(payload={"run_id": 30, "snapshot_id": "wrong", "state": "RECORDED"},
                             consumed_at=datetime(2026, 10, 15, 4, 0, 0))
        ]
        snapshot_query = MagicMock()
        snapshot_query.filter.return_value.all.return_value = [
            SimpleNamespace(run_id=30, user_id=12, snapshot_id="snapshot-30",
                            experiment_id=5, project_id="project-1", model_id="model-2",
                            state="RECORDED", code_sha256="a" * 64)
        ]
        db.query.side_effect = [professor_query, student_query, run_query, entry_query, snapshot_query]

        result = list_professor_ledger(db, SimpleNamespace(id=9, role="PROFESSOR"))

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
