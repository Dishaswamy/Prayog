import unittest
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mlflow_outbox_worker import _consume_run_sealed, _load_handlers, main


class MLFlowOutboxWorkerTests(unittest.TestCase):
    @patch("mlflow_outbox_worker.SessionLocal")
    @patch("mlflow_outbox_worker.consume_ledger_event", return_value=True)
    def test_run_sealed_handler_persists_event_and_closes_session(self, consume, session_local):
        db = session_local.return_value
        payload = {"state": "RECORDED", "snapshot_id": "snapshot-1"}
        result = _consume_run_sealed("run:12:sealed", payload)
        self.assertTrue(result)
        consume.assert_called_once_with(db, "run:12:sealed", payload)
        db.close.assert_called_once()

    @patch.dict("os.environ", {"PRAYOG_OUTBOX_HANDLER_MODULE": "configured_handlers"})
    @patch("mlflow_outbox_worker.importlib.import_module")
    def test_load_handlers_includes_builtin_run_sealed_consumer(self, import_module):
        steward = MagicMock()
        optimization = MagicMock()
        import_module.return_value.HANDLERS = {
            "STEWARD_DIAGNOSIS_REQUESTED": steward,
            "OPTIMIZATION_REQUESTED": optimization,
        }
        handlers = _load_handlers()
        self.assertIs(handlers["STEWARD_DIAGNOSIS_REQUESTED"], steward)
        self.assertIs(handlers["OPTIMIZATION_REQUESTED"], optimization)
        self.assertTrue(callable(handlers["RUN_SEALED"]))

    @patch.dict("os.environ", {}, clear=True)
    def test_load_handlers_requires_configured_ai_consumers(self):
        with self.assertRaisesRegex(RuntimeError, "PRAYOG_OUTBOX_HANDLER_MODULE"):
            _load_handlers()


    @patch("mlflow_outbox_worker.SessionLocal")
    @patch("mlflow_outbox_worker.consume_ledger_event", side_effect=RuntimeError("ledger unavailable"))
    def test_run_sealed_handler_closes_session_when_consumer_fails(self, consume, session_local):
        db = session_local.return_value
        with self.assertRaisesRegex(RuntimeError, "ledger unavailable"):
            _consume_run_sealed("run:13:sealed", {"state": "RECORDED"})
        consume.assert_called_once_with(db, "run:13:sealed", {"state": "RECORDED"})
        db.close.assert_called_once()

    @patch.dict("os.environ", {"PRAYOG_OUTBOX_HANDLER_MODULE": "configured_handlers"})
    @patch("mlflow_outbox_worker.importlib.import_module")
    def test_load_handlers_rejects_non_dictionary_handler_registry(self, import_module):
        import_module.return_value = SimpleNamespace(HANDLERS=None)
        with self.assertRaisesRegex(RuntimeError, "HANDLERS dictionary"):
            _load_handlers()

    @patch.dict("os.environ", {"PRAYOG_OUTBOX_HANDLER_MODULE": "configured_handlers"})
    @patch("mlflow_outbox_worker.importlib.import_module")
    def test_load_handlers_rejects_missing_or_non_callable_required_handlers(self, import_module):
        import_module.return_value = SimpleNamespace(
            HANDLERS={"STEWARD_DIAGNOSIS_REQUESTED": MagicMock()}
        )
        with self.assertRaisesRegex(RuntimeError, "callable handlers"):
            _load_handlers()

        import_module.return_value = SimpleNamespace(
            HANDLERS={
                "STEWARD_DIAGNOSIS_REQUESTED": MagicMock(),
                "OPTIMIZATION_REQUESTED": None,
            }
        )
        with self.assertRaisesRegex(RuntimeError, "callable handlers"):
            _load_handlers()

    @patch.dict("os.environ", {"PRAYOG_OUTBOX_POLL_SECONDS": "not-an-int"})
    @patch("mlflow_outbox_worker._load_handlers")
    def test_main_rejects_non_integer_poll_interval(self, load_handlers):
        with patch.object(sys, "argv", ["mlflow_outbox_worker.py", "--once"]):
            with self.assertRaisesRegex(RuntimeError, "must be an integer"):
                main()
        load_handlers.assert_not_called()

    @patch.dict("os.environ", {"PRAYOG_OUTBOX_POLL_SECONDS": "61"})
    @patch("mlflow_outbox_worker._load_handlers")
    def test_main_rejects_poll_interval_above_maximum(self, load_handlers):
        with patch.object(sys, "argv", ["mlflow_outbox_worker.py", "--once"]):
            with self.assertRaisesRegex(RuntimeError, "between 1 and 60"):
                main()
        load_handlers.assert_not_called()

    @patch("mlflow_outbox_worker.SessionLocal")
    @patch("mlflow_outbox_worker.dispatch_once", return_value=[("event-1", "DELIVERED")])
    @patch("mlflow_outbox_worker._load_handlers", return_value={"RUN_SEALED": MagicMock()})
    @patch.dict("os.environ", {"PRAYOG_OUTBOX_WORKER_ID": "worker-test"})
    def test_main_once_processes_batch_and_closes_session(self, load_handlers, dispatch, session_local):
        db = session_local.return_value
        with patch.object(sys, "argv", ["mlflow_outbox_worker.py", "--once", "--limit", "7"]):
            result = main()

        self.assertEqual(result, 0)
        dispatch.assert_called_once_with(db, "worker-test", load_handlers.return_value, limit=7)
        db.close.assert_called_once()
        db.rollback.assert_not_called()

    @patch("mlflow_outbox_worker.SessionLocal")
    @patch("mlflow_outbox_worker.dispatch_once", side_effect=RuntimeError("sensitive exception detail"))
    @patch("mlflow_outbox_worker._load_handlers", return_value={})
    @patch.dict("os.environ", {"PRAYOG_OUTBOX_WORKER_ID": "worker-test"})
    def test_main_once_rolls_back_and_returns_failure_when_batch_raises(self, load_handlers, dispatch, session_local):
        db = session_local.return_value
        with patch.object(sys, "argv", ["mlflow_outbox_worker.py", "--once"]):
            result = main()

        self.assertEqual(result, 1)
        db.rollback.assert_called_once()
        db.close.assert_called_once()

    @patch("mlflow_outbox_worker.SessionLocal")
    @patch("mlflow_outbox_worker.dispatch_once", return_value=[])
    @patch("mlflow_outbox_worker._load_handlers", return_value={})
    @patch.dict("os.environ", {"PRAYOG_OUTBOX_WORKER_ID": "w" * 100})
    def test_main_truncates_overlong_worker_identifier(self, load_handlers, dispatch, session_local):
        with patch.object(sys, "argv", ["mlflow_outbox_worker.py", "--once"]):
            self.assertEqual(main(), 0)
        self.assertEqual(len(dispatch.call_args.args[1]), 80)


if __name__ == "__main__":
    unittest.main()
