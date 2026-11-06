import hashlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret")
os.environ.setdefault("POSTGRES_USER", "prayog")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "prayog_test")

import jwt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from logic.auth import ALGORITHM, SECRET_KEY
from logic.dependencies import get_current_user, require_roles


class AuthenticationDependencyTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=17, user_id="student", role="STUDENT", session_id=None)
        self.db = MagicMock()
        self.db.query.return_value.filter.return_value.first.return_value = self.user

    def token(self, **overrides):
        payload = {
            "sub": "17",
            "jti": "session-123",
            "role": "STUDENT",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        payload.update(overrides)
        return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    def credentials(self, token):
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    def test_valid_token_resolves_user_from_database(self):
        result = get_current_user(self.credentials(self.token()), self.db)

        self.assertIs(result, self.user)
        self.db.query.return_value.filter.assert_called_once()
        self.assertEqual(self.user.session_id, "session-123")

    def test_database_role_is_authoritative_over_token_role(self):
        token = self.token(role="ADMIN")

        result = get_current_user(self.credentials(token), self.db)

        self.assertEqual(result.role, "STUDENT")
        self.assertEqual(result.session_id, "session-123")

    def test_missing_jti_uses_token_digest_as_session_id(self):
        token = self.token(jti=None)

        result = get_current_user(self.credentials(token), self.db)

        expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.assertEqual(result.session_id, expected)

    def test_expired_token_returns_unauthorized(self):
        token = self.token(exp=datetime.now(timezone.utc) - timedelta(minutes=1))

        with self.assertRaises(HTTPException) as raised:
            get_current_user(self.credentials(token), self.db)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Token has expired")
        self.db.query.assert_not_called()

    def test_malformed_token_returns_unauthorized(self):
        with self.assertRaises(HTTPException) as raised:
            get_current_user(self.credentials("not-a-jwt"), self.db)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Invalid authentication token")
        self.db.query.assert_not_called()

    def test_token_without_subject_returns_unauthorized(self):
        payload = {
            "jti": "session-123",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

        with self.assertRaises(HTTPException) as raised:
            get_current_user(self.credentials(token), self.db)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Invalid authentication token")
        self.db.query.assert_not_called()

    def test_non_numeric_subject_returns_unauthorized(self):
        token = self.token(sub="not-a-user-id")

        with self.assertRaises(HTTPException) as raised:
            get_current_user(self.credentials(token), self.db)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Invalid authentication token")
        self.db.query.assert_not_called()

    def test_token_signed_with_another_secret_returns_unauthorized(self):
        payload = {
            "sub": "17",
            "jti": "session-123",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        token = jwt.encode(payload, "different-test-secret", algorithm=ALGORITHM)

        with self.assertRaises(HTTPException) as raised:
            get_current_user(self.credentials(token), self.db)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Invalid authentication token")
        self.db.query.assert_not_called()

    def test_deleted_user_returns_unauthorized(self):
        self.db.query.return_value.filter.return_value.first.return_value = None

        with self.assertRaises(HTTPException) as raised:
            get_current_user(self.credentials(self.token()), self.db)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "User not found")

    def test_role_guard_allows_matching_database_role(self):
        checker = require_roles("STUDENT", "RESEARCHER")

        self.assertIs(checker(self.user), self.user)

    def test_role_guard_rejects_nonmatching_database_role(self):
        checker = require_roles("ADMIN")

        with self.assertRaises(HTTPException) as raised:
            checker(self.user)

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "Requires role: ADMIN")


if __name__ == "__main__":
    unittest.main()
