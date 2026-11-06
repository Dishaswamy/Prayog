import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from logic.mlflow.outbox import consume_ledger_event, enqueue_run_sealed_event, _still_eligible


class MLFlowLedgerConsumerTests(unittest.TestCase):
    def test_run_sealed_event_has_stable_idempotency_key_and_minimal_payload(self):
        db = MagicMock()
        snapshot = SimpleNamespace(
            snapshot_id="snapshot-12", experiment_id=4, project_id="project-2",
            model_id="model-3", state="RECORDED", code_sha256="a" * 64,
        )
        run = SimpleNamespace(id=21)
        event = enqueue_run_sealed_event(db, snapshot, run, 8)
        self.assertEqual(event.event_key, "run:21:sealed")
        self.assertEqual(event.event_type, "RUN_SEALED")
        self.assertEqual(event.run_id, 21)
        self.assertEqual(event.payload["snapshot_id"], "snapshot-12")
        self.assertNotIn("code", event.payload)
        db.add.assert_called_once_with(event)

    def test_ledger_consumer_records_new_event_once(self):
        db = MagicMock()
        db.commit.return_value = None
        self.assertTrue(consume_ledger_event(db, "run:21:sealed", {"state": "RECORDED"}))
        db.add.assert_called_once()
        db.commit.assert_called_once()

    def test_ledger_consumer_returns_false_for_duplicate_key(self):
        from sqlalchemy.exc import IntegrityError
        db = MagicMock()
        db.commit.side_effect = IntegrityError("duplicate", {}, Exception())
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(event_key="run:21:sealed")
        self.assertFalse(consume_ledger_event(db, "run:21:sealed", {"state": "RECORDED"}))
        db.rollback.assert_called_once()

    def test_ledger_consumer_rejects_unsealed_payload(self):
        db = MagicMock()
        with self.assertRaises(ValueError):
            consume_ledger_event(db, "run:21:sealed", {"state": "INCOMPLETE"})
        db.add.assert_not_called()

    @patch("logic.mlflow.outbox.ModelRun")
    @patch("logic.mlflow.outbox.ModelSnapshot")
    def test_run_sealed_event_is_only_eligible_for_matching_recorded_snapshot(self, snapshot_model, run_model):
        db = MagicMock()
        event = SimpleNamespace(
            event_type="RUN_SEALED", event_key="run:21:sealed", run_id=21, aggregate_id=None,
            user_id=8, payload={"run_id": 21, "snapshot_id": "snapshot-12", "state": "RECORDED"},
        )
        snapshot = SimpleNamespace(run_id=21, state="RECORDED")
        run = SimpleNamespace(id=21)
        db.query.side_effect = [
            SimpleNamespace(filter=MagicMock(return_value=SimpleNamespace(first=MagicMock(return_value=snapshot)))),
            SimpleNamespace(filter=MagicMock(return_value=SimpleNamespace(first=MagicMock(return_value=run)))),
        ]
        self.assertTrue(_still_eligible(db, event))


    def test_ledger_consumer_accepts_maximum_event_key_length(self):
        db = MagicMock()
        event_key = "k" * 160

        self.assertTrue(consume_ledger_event(db, event_key, {"state": "RECORDED"}))
        db.add.assert_called_once()
        self.assertEqual(db.add.call_args.args[0].event_key, event_key)
        db.commit.assert_called_once()

    def test_duplicate_integrity_error_without_matching_row_is_reraised(self):
        from sqlalchemy.exc import IntegrityError

        db = MagicMock()
        db.commit.side_effect = IntegrityError("unique constraint", {}, Exception())
        db.query.return_value.filter.return_value.first.return_value = None

        with self.assertRaises(IntegrityError):
            consume_ledger_event(db, "run:21:sealed", {"state": "RECORDED"})

        db.rollback.assert_called_once()

    def test_run_sealed_event_rejects_lifecycle_and_identity_mismatches(self):
        base_payload = {
            "run_id": 21,
            "snapshot_id": "snapshot-12",
            "state": "RECORDED",
        }
        cases = [
            ("incomplete_snapshot", "INCOMPLETE", 21, True, "RECORDED", "run:21:sealed"),
            ("snapshot_linked_to_other_run", "RECORDED", 99, True, "RECORDED", "run:21:sealed"),
            ("missing_run", "RECORDED", 21, False, "RECORDED", "run:21:sealed"),
            ("wrong_payload_state", "RECORDED", 21, True, "INCOMPLETE", "run:21:sealed"),
            ("wrong_event_key", "RECORDED", 21, True, "RECORDED", "run:99:sealed"),
        ]

        for name, snapshot_state, snapshot_run_id, run_exists, payload_state, event_key in cases:
            with self.subTest(case=name):
                db = MagicMock()
                snapshot = SimpleNamespace(
                    run_id=snapshot_run_id,
                    state=snapshot_state,
                )
                run = SimpleNamespace(id=21) if run_exists else None
                snapshot_query = MagicMock()
                snapshot_query.filter.return_value.first.return_value = snapshot
                run_query = MagicMock()
                run_query.filter.return_value.first.return_value = run
                db.query.side_effect = [snapshot_query, run_query]
                event = SimpleNamespace(
                    event_type="RUN_SEALED",
                    event_key=event_key,
                    run_id=21,
                    aggregate_id=None,
                    user_id=8,
                    payload={**base_payload, "state": payload_state},
                )

                self.assertFalse(_still_eligible(db, event))
                self.assertEqual(db.query.call_count, 2)


if __name__ == "__main__":
    unittest.main()
