import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError

from logic.errors import BadRequestError, ConflictError
from logic.mlflow import log_metrics


class MLFlowMetricBatchTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=7)
        self.run = SimpleNamespace(id=3, user_id=7, status="RUNNING")
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = self.run

    def test_batch_is_persisted_in_one_transaction(self):
        result = log_metrics(
            self.db,
            3,
            self.user,
            [
                {"name": "loss", "value": 0.7, "step": 1},
                {"name": "accuracy", "value": 0.9, "step": 1},
            ],
        )

        self.assertEqual(len(result), 2)
        self.assertEqual([(m.name, m.value, m.step) for m in result], [
            ("loss", 0.7, 1),
            ("accuracy", 0.9, 1),
        ])
        self.db.add_all.assert_called_once_with(result)
        self.db.commit.assert_called_once()
        self.assertEqual(self.db.refresh.call_count, 2)

    def test_duplicate_name_and_step_in_batch_is_rejected_before_write(self):
        with self.assertRaisesRegex(ConflictError, "appears more than once"):
            log_metrics(
                self.db,
                3,
                self.user,
                [
                    {"name": "loss", "value": 0.7, "step": 1},
                    {"name": "loss", "value": 0.6, "step": 1},
                ],
            )

        self.db.add_all.assert_not_called()
        self.db.commit.assert_not_called()

    def test_batch_requires_running_run(self):
        self.run.status = "SUCCEEDED"

        with self.assertRaisesRegex(ConflictError, "RUNNING"):
            log_metrics(self.db, 3, self.user, [{"name": "loss", "value": 0.7, "step": 1}])

        self.db.add_all.assert_not_called()
        self.db.commit.assert_not_called()

    def test_database_duplicate_rolls_back_entire_batch(self):
        self.db.commit.side_effect = IntegrityError("insert", {}, Exception("duplicate"))

        with self.assertRaisesRegex(ConflictError, "already exists"):
            log_metrics(
                self.db,
                3,
                self.user,
                [
                    {"name": "loss", "value": 0.7, "step": 1},
                    {"name": "accuracy", "value": 0.9, "step": 1},
                ],
            )

        self.db.add_all.assert_called_once()
        self.db.rollback.assert_called_once()

    def test_non_finite_values_are_rejected_before_write(self):
        with self.assertRaisesRegex(BadRequestError, "finite"):
            log_metrics(self.db, 3, self.user, [{"name": "loss", "value": float("nan"), "step": 1}])

        self.db.add_all.assert_not_called()
        self.db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
