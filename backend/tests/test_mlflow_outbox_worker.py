import unittest
from unittest.mock import MagicMock, patch

from mlflow_outbox_worker import _consume_run_sealed, _load_handlers


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


if __name__ == "__main__":
    unittest.main()
