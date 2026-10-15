import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from mlflow_worker import claim_next_job


class SharedQueueOrderingTests(unittest.TestCase):
    def make_candidate_query(self, candidate):
        query = MagicMock()
        query.filter.return_value.order_by.return_value.first.return_value = candidate
        return query

    def make_lock_query(self, candidate):
        query = MagicMock()
        query.filter.return_value.with_for_update.return_value.first.return_value = candidate
        return query

    def test_older_rebuild_is_claimed_before_newer_first_time_run(self):
        now = datetime(2026, 10, 14, 12, 0, 0)
        run = SimpleNamespace(
            id=41, created_at=now, started_at=now + timedelta(minutes=2),
            status="QUEUED", run_type="FIRST_TIME", code_artifact_id="code-1",
            worker_id=None, worker_claimed_at=None,
        )
        attempt = SimpleNamespace(
            id=9, created_at=now, initiated_at=now, state="REBUILD_INITIATED",
            worker_id=None, worker_claimed_at=None,
        )
        db = MagicMock()
        db.query.side_effect = [
            self.make_candidate_query(run),
            self.make_candidate_query(attempt),
            self.make_lock_query(attempt),
        ]

        result = claim_next_job(db, "worker-a")

        self.assertEqual(result, ("REBUILD", 9))
        self.assertEqual(attempt.worker_id, "worker-a")
        self.assertIsNotNone(attempt.worker_claimed_at)
        self.assertEqual(run.status, "QUEUED")
        db.commit.assert_called_once()

    def test_older_first_time_run_is_claimed_before_newer_rebuild(self):
        now = datetime(2026, 10, 14, 12, 0, 0)
        run = SimpleNamespace(
            id=41, created_at=now, started_at=now,
            status="QUEUED", run_type="FIRST_TIME", code_artifact_id="code-1",
            worker_id=None, worker_claimed_at=None,
        )
        attempt = SimpleNamespace(
            id=9, created_at=now, initiated_at=now + timedelta(minutes=2),
            state="REBUILD_INITIATED", worker_id=None, worker_claimed_at=None,
        )
        db = MagicMock()
        db.query.side_effect = [
            self.make_candidate_query(run),
            self.make_candidate_query(attempt),
            self.make_lock_query(run),
        ]

        result = claim_next_job(db, "worker-a")

        self.assertEqual(result, ("FIRST_TIME", 41))
        self.assertEqual(run.status, "RUNNING")
        self.assertEqual(run.worker_id, "worker-a")
        self.assertIsNotNone(run.worker_claimed_at)
        self.assertEqual(attempt.state, "REBUILD_INITIATED")
        db.commit.assert_called_once()

    def test_empty_shared_queue_returns_none(self):
        db = MagicMock()
        db.query.side_effect = [
            self.make_candidate_query(None),
            self.make_candidate_query(None),
        ]

        self.assertIsNone(claim_next_job(db, "worker-a"))
        db.rollback.assert_called_once()
        db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
