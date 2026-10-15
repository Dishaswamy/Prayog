import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from logic.errors import BadRequestError, ConflictError
from logic.mlflow_compare import compare_rebuild_metrics


class MetricComparatorTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=7)
        self.rebuild = SimpleNamespace(
            id=12,
            state="REBUILD_SUCCESS",
            snapshot_id="snapshot-4",
            execution_run_id=22,
        )
        self.snapshot = SimpleNamespace(snapshot_id="snapshot-4", state="RECORDED", run_id=11)
        self.rebuild_query = MagicMock()
        self.snapshot_query = MagicMock()
        self.source_query = MagicMock()
        self.rebuilt_query = MagicMock()
        self.db.query.side_effect = [
            self.rebuild_query, self.snapshot_query, self.source_query, self.rebuilt_query
        ]
        self.rebuild_query.filter.return_value.first.return_value = self.rebuild
        self.snapshot_query.filter.return_value.first.return_value = self.snapshot

    def compare(self, source, rebuilt, tolerances):
        self.source_query.filter.return_value.all.return_value = source
        self.rebuilt_query.filter.return_value.all.return_value = rebuilt
        return compare_rebuild_metrics(self.db, 12, self.user, tolerances)

    def test_metrics_within_per_metric_tolerance_pass(self):
        result = self.compare(
            [SimpleNamespace(name="loss", step=1, value=0.5)],
            [SimpleNamespace(name="loss", step=1, value=0.53)],
            {"loss": 0.05},
        )
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["metrics"][0]["absolute_difference"], 0.03)
        self.assertEqual(result["metrics"][0]["tolerance"], 0.05)

    def test_metrics_outside_tolerance_fail(self):
        result = self.compare(
            [SimpleNamespace(name="loss", step=1, value=0.5)],
            [SimpleNamespace(name="loss", step=1, value=0.7)],
            {"loss": 0.05},
        )
        self.assertFalse(result["passed"])
        self.assertFalse(result["metrics"][0]["passed"])

    def test_missing_metric_fails_comparison(self):
        result = self.compare(
            [SimpleNamespace(name="loss", step=1, value=0.5)],
            [],
            {"loss": 0.05},
        )
        self.assertFalse(result["passed"])
        self.assertIsNone(result["metrics"][0]["actual"])

    def test_no_metrics_is_not_a_pass(self):
        result = self.compare([], [], {})
        self.assertFalse(result["passed"])
        self.assertEqual(result["metrics_compared"], 0)

    def test_only_successful_rebuilds_can_be_compared(self):
        self.rebuild.state = "REBUILD_FAILED"
        with self.assertRaises(ConflictError):
            compare_rebuild_metrics(self.db, 12, self.user, {})
        self.snapshot_query.filter.return_value.first.assert_not_called()

    def test_invalid_tolerance_rejected(self):
        with self.assertRaises(BadRequestError):
            compare_rebuild_metrics(self.db, 12, self.user, {"loss": -0.1})


if __name__ == "__main__":
    unittest.main()
