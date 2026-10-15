import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from logic.errors import BadRequestError
from logic.steward_context import retrieve_context, store_context_chunk
from mlflow_models import RebuildAttempt, StewardContextChunk


class StewardContextIsolationTests(unittest.TestCase):
    def test_unscoped_retrieval_returns_zero_rows_without_querying_database(self):
        db = MagicMock()

        self.assertEqual(retrieve_context(db, None, 11, query="import error"), [])
        self.assertEqual(retrieve_context(db, 7, None, query="import error"), [])
        db.query.assert_not_called()

    def test_retrieval_is_scoped_by_user_and_attempt_in_database(self):
        attempt = SimpleNamespace(id=11, user_id=7, state="REBUILD_FAILED")
        chunk = SimpleNamespace(
            id="chunk-1", user_id=7, rebuild_id=11, source_type="DIAGNOSTIC_LOG",
            content="ModuleNotFoundError while importing numpy", embedding=None,
            created_at=datetime(2026, 10, 14),
        )
        db = MagicMock()
        attempt_query = MagicMock()
        attempt_query.filter.return_value.first.return_value = attempt
        chunk_query = MagicMock()
        chunk_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [chunk]
        db.query.side_effect = [attempt_query, chunk_query]

        result = retrieve_context(db, 7, 11, query="numpy import", top_k=3)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["attempt_id"], 11)
        self.assertEqual(result[0]["source_type"], "DIAGNOSTIC_LOG")
        self.assertGreater(result[0]["score"], 0)
        self.assertEqual(len(chunk_query.filter.call_args.args), 2)
        self.assertTrue(any("user_id" in str(arg) for arg in chunk_query.filter.call_args.args))
        self.assertTrue(any("rebuild_id" in str(arg) for arg in chunk_query.filter.call_args.args))

    def test_foreign_or_nonfailed_attempt_returns_empty_context(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        self.assertEqual(retrieve_context(db, 7, 99, query="failure"), [])
        db.query.assert_called_once_with(RebuildAttempt)
        db.add.assert_not_called()

    def test_ingestion_requires_explicit_sanitization_and_failed_owned_attempt(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=11, user_id=7, state="REBUILD_FAILED"
        )

        with self.assertRaisesRegex(BadRequestError, "Only sanitized context"):
            store_context_chunk(db, 7, 11, "DIAGNOSTIC_LOG", "raw log", sanitized=False)
        db.add.assert_not_called()

        chunk = store_context_chunk(
            db, 7, 11, "DIAGNOSTIC_LOG", "sanitized import failure",
            embedding=[0.2, 0.8], sanitized=True,
        )
        self.assertEqual(chunk.user_id, 7)
        self.assertEqual(chunk.rebuild_id, 11)
        self.assertEqual(chunk.embedding, [0.2, 0.8])
        db.commit.assert_called_once()

    def test_invalid_embeddings_are_rejected(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=11, user_id=7, state="REBUILD_FAILED"
        )
        with self.assertRaisesRegex(BadRequestError, "finite numbers"):
            store_context_chunk(
                db, 7, 11, "MLFLOW_METADATA", "sanitized metadata",
                embedding=[float("nan")], sanitized=True,
            )
        db.add.assert_not_called()
        db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
