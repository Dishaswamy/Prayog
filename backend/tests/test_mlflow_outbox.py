import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from logic.mlflow_outbox import (
    claim_events,
    dispatch_once,
    enqueue_rebuild_terminal_event,
    mark_delivery_failed,
    mark_delivered,
    enqueue_run_sealed_event,
)


class MLFlowOutboxTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.attempt = SimpleNamespace(id=17)
        self.snapshot = SimpleNamespace(snapshot_id="snapshot-9")
        self.execution = SimpleNamespace(id=31)
        self.log = SimpleNamespace(id="log-1")

    def test_failed_rebuild_without_verified_log_does_not_enqueue_diagnosis(self):
        event = enqueue_rebuild_terminal_event(
            self.db, self.attempt, self.snapshot, self.execution, 5,
            "REBUILD_FAILED", None,
        )
        self.assertIsNone(event)
        self.db.add.assert_not_called()

    def test_failed_rebuild_with_verified_log_enqueues_diagnosis_only(self):
        event = enqueue_rebuild_terminal_event(
            self.db, self.attempt, self.snapshot, self.execution, 5,
            "REBUILD_FAILED", self.log,
        )
        self.assertEqual(event.event_type, "STEWARD_DIAGNOSIS_REQUESTED")
        self.assertEqual(event.event_key, "rebuild:17:steward-diagnosis")
        self.assertEqual(event.payload["log_artifact_id"], "log-1")
        self.assertEqual(event.payload["terminal_state"], "REBUILD_FAILED")
        self.assertNotIn("source_code", event.payload)
        self.db.add.assert_called_once_with(event)

    def test_successful_rebuild_enqueues_optimization_without_source_artifact_content(self):
        event = enqueue_rebuild_terminal_event(
            self.db, self.attempt, self.snapshot, self.execution, 5,
            "REBUILD_SUCCESS", None,
        )
        self.assertEqual(event.event_type, "OPTIMIZATION_REQUESTED")
        self.assertEqual(event.event_key, "rebuild:17:optimization")
        self.assertIsNone(event.payload["log_artifact_id"])
        self.assertEqual(event.payload["terminal_state"], "REBUILD_SUCCESS")
        self.db.add.assert_called_once_with(event)

    def test_cancelled_rebuild_does_not_enqueue_ai_work(self):
        event = enqueue_rebuild_terminal_event(
            self.db, self.attempt, self.snapshot, self.execution, 5,
            "REBUILD_CANCELLED", self.log,
        )
        self.assertIsNone(event)
        self.db.add.assert_not_called()

    @patch("logic.mlflow_outbox._still_eligible", return_value=True)
    @patch("logic.mlflow_outbox.mark_delivered", return_value=True)
    @patch("logic.mlflow_outbox.claim_events")
    def test_dispatch_uses_idempotency_key_and_marks_delivery(self, claim, delivered, eligible):
        event = SimpleNamespace(
            id="event-1",
            event_type="STEWARD_DIAGNOSIS_REQUESTED",
            event_key="rebuild:17:steward-diagnosis",
            payload={"rebuild_id": 17, "terminal_state": "REBUILD_FAILED"},
        )
        claim.return_value = [event]
        handler = MagicMock()

        result = dispatch_once(
            self.db, "dispatcher-1",
            {"STEWARD_DIAGNOSIS_REQUESTED": handler},
        )

        handler.assert_called_once_with(event.event_key, event.payload)
        delivered.assert_called_once_with(self.db, event.id, "dispatcher-1")
        self.assertEqual(result, [("event-1", "DELIVERED")])

    @patch("logic.mlflow_outbox._still_eligible", return_value=True)
    @patch("logic.mlflow_outbox.mark_delivery_failed", return_value=True)
    @patch("logic.mlflow_outbox.claim_events")
    def test_handler_failure_is_scheduled_for_retry(self, claim, failed, eligible):
        event = SimpleNamespace(
            id="event-2", event_type="OPTIMIZATION_REQUESTED",
            event_key="rebuild:18:optimization", payload={"rebuild_id": 18},
        )
        claim.return_value = [event]
        handler = MagicMock(side_effect=RuntimeError("service unavailable"))

        result = dispatch_once(
            self.db, "dispatcher-1", {"OPTIMIZATION_REQUESTED": handler}
        )

        failed.assert_called_once()
        self.assertEqual(failed.call_args.args[:3], (self.db, "event-2", "dispatcher-1"))
        self.assertIn("RuntimeError", failed.call_args.args[3])
        self.assertNotIn("service unavailable", failed.call_args.args[3])
        self.assertEqual(result, [("event-2", "RETRY_SCHEDULED")])

    @patch("logic.mlflow_outbox.mark_delivery_failed", return_value=True)
    @patch("logic.mlflow_outbox._still_eligible", return_value=False)
    @patch("logic.mlflow_outbox.claim_events")
    def test_ineligible_event_is_dead_lettered_without_calling_handler(self, claim, eligible, failed):
        event = SimpleNamespace(
            id="event-3", event_type="STEWARD_DIAGNOSIS_REQUESTED",
            event_key="rebuild:19:steward-diagnosis", payload={"rebuild_id": 19},
        )
        claim.return_value = [event]
        handler = MagicMock()

        result = dispatch_once(
            self.db, "dispatcher-1", {"STEWARD_DIAGNOSIS_REQUESTED": handler}
        )

        handler.assert_not_called()
        failed.assert_called_once_with(
            self.db, "event-3", "dispatcher-1",
            "Persisted lifecycle state or required artifact is no longer eligible.",
            max_attempts=1,
        )
        self.assertEqual(result, [("event-3", "INELIGIBLE")])

    @patch("logic.mlflow_outbox._now", return_value=datetime(2026, 1, 1))
    def test_claim_sets_lease_and_increments_attempts(self, now):
        event = SimpleNamespace(
            id="event-4", status="PENDING", attempts=0, locked_by=None,
            locked_until=None,
        )
        query = self.db.query.return_value
        query.filter.return_value = query
        query.order_by.return_value = query
        query.with_for_update.return_value = query
        query.limit.return_value = query
        query.all.return_value = [event]

        result = claim_events(self.db, "worker-1", limit=5, lease_seconds=30)

        self.assertEqual(result, [event])
        self.assertEqual(event.status, "PROCESSING")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.locked_by, "worker-1")
        self.assertEqual(event.locked_until, datetime(2026, 1, 1) + timedelta(seconds=30))
        self.db.commit.assert_called_once()

    @patch("logic.mlflow_outbox._now", return_value=datetime(2026, 1, 1))
    def test_delivery_failure_uses_backoff_and_sanitizes_error(self, now):
        event = SimpleNamespace(
            id="event-5", status="PROCESSING", attempts=2, locked_by="worker-1",
            locked_until=datetime(2026, 1, 1) + timedelta(seconds=30),
            available_at=datetime(2025, 1, 1), last_error=None,
        )
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = event

        changed = mark_delivery_failed(
            self.db, "event-5", "worker-1", "RuntimeError: handler delivery failed"
        )

        self.assertTrue(changed)
        self.assertEqual(event.status, "PENDING")
        self.assertEqual(event.available_at, datetime(2026, 1, 1) + timedelta(seconds=4))
        self.assertEqual(event.last_error, "RuntimeError: handler delivery failed")
        self.assertIsNone(event.locked_by)
        self.db.commit.assert_called_once()

    @patch("logic.mlflow_outbox._now", return_value=datetime(2026, 1, 1))
    def test_delivery_failure_dead_letters_after_max_attempts(self, now):
        event = SimpleNamespace(
            id="event-6", status="PROCESSING", attempts=5, locked_by="worker-1",
            locked_until=datetime(2026, 1, 1) + timedelta(seconds=30),
            available_at=datetime(2025, 1, 1), last_error=None,
        )
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = event

        changed = mark_delivery_failed(
            self.db, "event-6", "worker-1", "handler failed"
        )

        self.assertTrue(changed)
        self.assertEqual(event.status, "DEAD")
        self.assertIsNone(event.locked_by)
        self.assertEqual(event.last_error, "handler failed")
        self.db.commit.assert_called_once()


class MLFlowOutboxBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    def test_claim_events_rejects_invalid_public_arguments(self):
        invalid = [
            ("", 10, 60),
            (None, 10, 60),
            ("x" * 81, 10, 60),
            ("worker", 0, 60),
            ("worker", 101, 60),
            ("worker", 10, 9),
            ("worker", 10, 3601),
        ]
        for worker_id, limit, lease_seconds in invalid:
            with self.subTest(worker_id=worker_id, limit=limit, lease_seconds=lease_seconds):
                with self.assertRaises(ValueError):
                    claim_events(self.db, worker_id, limit=limit, lease_seconds=lease_seconds)

    def test_claim_events_accepts_documented_argument_boundaries(self):
        query = self.db.query.return_value
        query.filter.return_value = query
        query.order_by.return_value = query
        query.with_for_update.return_value = query
        query.limit.return_value = query
        query.all.return_value = []

        self.assertEqual(claim_events(self.db, "w", limit=1, lease_seconds=10), [])
        self.assertEqual(
            claim_events(self.db, "w" * 80, limit=100, lease_seconds=3600),
            [],
        )
        self.assertEqual(self.db.commit.call_count, 2)

    def test_mark_delivered_returns_false_when_lease_is_lost(self):
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = None

        self.assertFalse(mark_delivered(self.db, "event-1", "another-worker"))
        self.db.commit.assert_not_called()

    def test_mark_delivery_failed_returns_false_when_lease_is_lost(self):
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = None

        self.assertFalse(
            mark_delivery_failed(self.db, "event-1", "another-worker", "failure")
        )
        self.db.commit.assert_not_called()

    @patch("logic.mlflow_outbox._now", return_value=datetime(2026, 1, 1))
    def test_delivery_failure_sanitizes_empty_and_oversized_errors(self, now):
        event = SimpleNamespace(
            id="event-1",
            status="PROCESSING",
            attempts=1,
            locked_by="worker-1",
            locked_until=datetime(2026, 1, 1) + timedelta(seconds=30),
            available_at=datetime(2025, 1, 1),
            last_error=None,
        )
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = event

        self.assertTrue(mark_delivery_failed(self.db, "event-1", "worker-1", ""))
        self.assertEqual(event.last_error, "Delivery failed")

        event.status = "PROCESSING"
        event.attempts = 5
        event.locked_by = "worker-1"
        event.locked_until = datetime(2026, 1, 1) + timedelta(seconds=30)
        self.assertTrue(
            mark_delivery_failed(self.db, "event-1", "worker-1", "x" * 700)
        )
        self.assertEqual(len(event.last_error), 500)

    def test_mark_delivery_failed_rejects_invalid_max_attempts(self):
        for value in (0, -1, 21):
            with self.subTest(max_attempts=value):
                with self.assertRaises(ValueError):
                    mark_delivery_failed(
                        self.db, "event-1", "worker-1", "failure", max_attempts=value
                    )

    def test_consume_ledger_event_rejects_invalid_keys_and_payloads(self):
        from logic.mlflow_outbox import consume_ledger_event

        invalid = [
            ("", {"state": "RECORDED"}),
            ("x" * 161, {"state": "RECORDED"}),
            ("valid", None),
            ("valid", []),
            ("valid", {"state": "RUNNING"}),
        ]
        for event_key, payload in invalid:
            with self.subTest(event_key=event_key, payload=payload):
                with self.assertRaises(ValueError):
                    consume_ledger_event(self.db, event_key, payload)
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_consume_ledger_event_rolls_back_database_errors(self):
        from logic.mlflow_outbox import consume_ledger_event

        self.db.commit.side_effect = RuntimeError("database unavailable")
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            consume_ledger_event(self.db, "run:41:sealed", {"state": "RECORDED"})

        self.db.add.assert_called_once()
        self.db.rollback.assert_called_once()

    def test_enqueue_run_sealed_event_builds_expected_payload(self):
        snapshot = SimpleNamespace(
            snapshot_id="snapshot-41",
            experiment_id=7,
            project_id=8,
            model_id=9,
            state="RECORDED",
            code_sha256="abc123",
        )
        run = SimpleNamespace(id=41)

        event = enqueue_run_sealed_event(self.db, snapshot, run, user_id=12)

        self.assertEqual(event.event_key, "run:41:sealed")
        self.assertEqual(event.event_type, "RUN_SEALED")
        self.assertEqual(event.status, "PENDING")
        self.assertEqual(event.attempts, 0)
        self.assertEqual(
            event.payload,
            {
                "run_id": 41,
                "snapshot_id": "snapshot-41",
                "experiment_id": 7,
                "project_id": 8,
                "model_id": 9,
                "state": "RECORDED",
                "code_sha256": "abc123",
            },
        )
        self.assertEqual(event.user_id, 12)
        self.db.add.assert_called_once_with(event)


    @patch("logic.mlflow_outbox._now", return_value=datetime(2026, 1, 1))
    def test_mark_delivered_updates_terminal_fields_and_releases_lease(self, now):
        event = SimpleNamespace(
            id="event-delivered",
            status="PROCESSING",
            locked_by="worker-1",
            locked_until=datetime(2026, 1, 1) + timedelta(seconds=30),
            delivered_at=None,
            last_error="previous failure",
        )
        query = self.db.query.return_value.filter.return_value
        query.with_for_update.return_value.first.return_value = event

        self.assertTrue(mark_delivered(self.db, "event-delivered", "worker-1"))

        self.assertEqual(event.status, "DELIVERED")
        self.assertEqual(event.delivered_at, datetime(2026, 1, 1))
        self.assertIsNone(event.locked_by)
        self.assertIsNone(event.locked_until)
        self.assertIsNone(event.last_error)
        self.db.commit.assert_called_once()

    @patch("logic.mlflow_outbox._now", return_value=datetime(2026, 1, 1))
    def test_claim_events_reclaims_expired_processing_lease(self, now):
        event = SimpleNamespace(
            id="event-expired",
            status="PROCESSING",
            attempts=2,
            locked_by="dead-worker",
            locked_until=datetime(2025, 12, 31),
        )
        query = self.db.query.return_value
        query.filter.return_value = query
        query.order_by.return_value = query
        query.with_for_update.return_value = query
        query.limit.return_value = query
        query.all.return_value = [event]

        claimed = claim_events(self.db, "replacement-worker", limit=1, lease_seconds=10)

        self.assertEqual(claimed, [event])
        self.assertEqual(event.status, "PROCESSING")
        self.assertEqual(event.attempts, 3)
        self.assertEqual(event.locked_by, "replacement-worker")
        self.assertEqual(event.locked_until, datetime(2026, 1, 1) + timedelta(seconds=10))
        self.db.commit.assert_called_once()

    @patch("logic.mlflow_outbox.mark_delivery_failed", return_value=True)
    @patch("logic.mlflow_outbox._still_eligible", return_value=True)
    @patch("logic.mlflow_outbox.claim_events")
    def test_dispatch_without_handler_schedules_retry(self, claim, eligible, failed):
        event = SimpleNamespace(
            id="event-no-handler",
            event_type="UNREGISTERED_EVENT",
            event_key="event:no-handler",
            payload={"value": 1},
        )
        claim.return_value = [event]

        result = dispatch_once(self.db, "worker-1", {})

        self.assertEqual(result, [("event-no-handler", "RETRY_SCHEDULED")])
        self.db.rollback.assert_called_once()
        failed.assert_called_once_with(
            self.db,
            "event-no-handler",
            "worker-1",
            "RuntimeError: handler delivery failed",
        )


if __name__ == "__main__":
    unittest.main()
