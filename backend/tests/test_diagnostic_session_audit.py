import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from logic.diagnostic_links import consume_diagnostic_link, create_diagnostic_link
from logic.errors import ConflictError, NotFoundError


class DiagnosticSessionAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=7, session_id="session-a")
        self.session_hash = hashlib.sha256(b"session-a").hexdigest()

    def test_creation_requires_failed_attempt_and_audits_rejection(self):
        attempt = SimpleNamespace(
            id=11, user_id=7, state="REBUILD_SUCCESS", logs_artifact_id="log-1"
        )
        self.db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = attempt

        with self.assertRaises(ConflictError):
            create_diagnostic_link(self.db, 11, self.user, "session-a", 300)

        audit = self.db.add.call_args.args[0]
        self.assertEqual(audit.outcome, "STATE_REJECTED")
        self.assertEqual(audit.session_hash, self.session_hash)
        self.db.commit.assert_called_once()

    @patch("logic.diagnostic_links.get_verified_artifact")
    def test_creation_records_session_hash_and_created_audit(self, verify):
        attempt = SimpleNamespace(
            id=11, user_id=7, state="REBUILD_FAILED", logs_artifact_id="log-1",
            snapshot_id="snapshot-1",
        )
        snapshot = SimpleNamespace(snapshot_id="snapshot-1", user_id=7, experiment_id=2)
        artifact = SimpleNamespace(id="log-1", user_id=7, experiment_id=2, kind="LOG")
        queries = []
        for result in (attempt, snapshot, artifact):
            query = MagicMock()
            query.filter.return_value = query
            query.with_for_update.return_value = query
            query.first.return_value = result
            queries.append(query)
        self.db.query.side_effect = queries
        verify.return_value = (artifact, Path("/private/log.txt"))

        token, expires_at = create_diagnostic_link(self.db, 11, self.user, "session-a", 300)

        self.assertGreaterEqual(len(token), 40)
        self.assertGreater(expires_at, datetime.now(timezone.utc).replace(tzinfo=None))
        link = next(call.args[0] for call in self.db.add.call_args_list if hasattr(call.args[0], "token_hash"))
        audit = next(call.args[0] for call in self.db.add.call_args_list if getattr(call.args[0], "outcome", None) == "CREATED")
        self.assertEqual(link.session_hash, self.session_hash)
        self.assertEqual(audit.link_id, link.id)
        self.assertEqual(audit.outcome, "CREATED")
        self.db.commit.assert_called_once()

    @patch("logic.diagnostic_links.get_verified_artifact")
    def test_consumption_requires_same_user_session_and_audits_success(self, verify):
        token = "t" * 48
        link = SimpleNamespace(
            id="link-1", token_hash=hashlib.sha256(token.encode()).hexdigest(),
            session_hash=self.session_hash, user_id=7, rebuild_id=11, artifact_id="log-1",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5), consumed_at=None,
        )
        artifact = SimpleNamespace(id="log-1", user_id=7, experiment_id=2, kind="LOG")
        link_query = MagicMock()
        link_query.filter.return_value.with_for_update.return_value.first.return_value = link
        artifact_query = MagicMock()
        artifact_query.filter.return_value.first.return_value = artifact
        self.db.query.side_effect = [link_query, artifact_query]
        verify.return_value = (artifact, Path("/private/log.txt"))

        result, path = consume_diagnostic_link(self.db, token, self.user, "session-a")

        self.assertIs(result, artifact)
        self.assertEqual(str(path), "/private/log.txt")
        self.assertIsNotNone(link.consumed_at)
        audit = self.db.add.call_args.args[0]
        self.assertEqual(audit.outcome, "CONSUMED")
        self.db.commit.assert_called_once()

    def test_session_mismatch_is_denied_and_audited_without_reading_artifact(self):
        token = "t" * 48
        link = SimpleNamespace(
            id="link-1", token_hash=hashlib.sha256(token.encode()).hexdigest(),
            session_hash=hashlib.sha256(b"other-session").hexdigest(),
            user_id=7, rebuild_id=11, artifact_id="log-1",
            expires_at=datetime.utcnow() + timedelta(minutes=5), consumed_at=None,
        )
        self.db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = link

        with self.assertRaises(NotFoundError):
            consume_diagnostic_link(self.db, token, self.user, "session-a")

        audit = self.db.add.call_args.args[0]
        self.assertEqual(audit.outcome, "SESSION_MISMATCH")
        self.db.query.assert_called_once()
        self.db.commit.assert_called_once()

    def test_replay_is_denied_and_audited(self):
        token = "t" * 48
        link = SimpleNamespace(
            id="link-1", token_hash=hashlib.sha256(token.encode()).hexdigest(),
            session_hash=self.session_hash, user_id=7, rebuild_id=11, artifact_id="log-1",
            expires_at=datetime.utcnow() + timedelta(minutes=5), consumed_at=datetime.utcnow(),
        )
        self.db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = link

        with self.assertRaises(NotFoundError):
            consume_diagnostic_link(self.db, token, self.user, "session-a")

        self.assertEqual(self.db.add.call_args.args[0].outcome, "REPLAY")
        self.db.query.assert_called_once()
        self.db.commit.assert_called_once()

    def test_invalid_ttl_is_audited_before_rejection(self):
        with self.assertRaises(ConflictError):
            create_diagnostic_link(self.db, 11, self.user, "session-a", 901)

        audit = self.db.add.call_args.args[0]
        self.assertEqual(audit.outcome, "INVALID_TTL")
        self.assertEqual(audit.rebuild_id, 11)
        self.db.query.assert_not_called()
        self.db.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
