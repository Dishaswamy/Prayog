import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from routers.admin_mlflow import mlflow_overview, router


class AdminMLFlowOverviewTests(unittest.TestCase):
    def test_overview_contains_aggregates_only(self):
        db = MagicMock()
        query_results = []

        def scalar_query(value):
            query = MagicMock()
            query.select_from.return_value = query
            query.scalar.return_value = value
            query_results.append(query)

        def grouped_query(rows):
            query = MagicMock()
            query.group_by.return_value = query
            query.all.return_value = rows
            query_results.append(query)

        scalar_query(2048)
        scalar_query(4)
        scalar_query(3)
        scalar_query(5)
        scalar_query(9)
        grouped_query([("RUNNING", 3), ("SUCCEEDED", 6)])
        scalar_query(7)
        scalar_query(8)
        scalar_query(21)
        scalar_query(2)
        grouped_query([("REBUILD_SUCCESS", 5), ("REBUILD_FAILED", 1)])
        scalar_query(6)
        grouped_query([("DELIVERED", 8), ("PENDING", 1)])
        db.query.side_effect = query_results

        result = mlflow_overview(db=db, _=SimpleNamespace(role="ADMIN"))

        self.assertEqual(result["availability"]["database"], "available")
        self.assertEqual(result["volume"]["logical_artifact_bytes"], 2048)
        self.assertEqual(result["volume"]["runs_by_status"], {"RUNNING": 3, "SUCCEEDED": 6})
        self.assertEqual(result["volume"]["rebuilds_by_state"], {"REBUILD_SUCCESS": 5, "REBUILD_FAILED": 1})
        self.assertEqual(result["volume"]["outbox_events_by_status"], {"DELIVERED": 8, "PENDING": 1})
        sensitive_fields = {"source_code", "hyperparameters", "dataset_metadata", "storage_key", "description"}
        self.assertFalse(sensitive_fields & set(result))
        self.assertFalse(sensitive_fields & set(result["volume"]))

    def test_overview_route_requires_database_authenticated_admin(self):
        route = next(route for route in router.routes if route.path == "/admin/mlflow/overview")
        role_guard = next(
            dependency.call for dependency in route.dependant.dependencies
            if getattr(dependency.call, "__name__", "") == "checker"
        )
        with self.assertRaises(HTTPException) as raised:
            role_guard(SimpleNamespace(role="STUDENT"))
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
