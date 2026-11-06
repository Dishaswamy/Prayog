import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret")
os.environ.setdefault("POSTGRES_USER", "prayog")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "prayog_test")

from logic.auth.authentication import (
    authenticate_user,
    create_user_record,
    normalize_email,
    register_user,
)
from logic.common.errors import BadRequestError, ConflictError, ForbiddenError
from models import User


class AuthenticationLogicTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.query.return_value.filter.return_value.first.return_value = None
        self.db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = None

    def test_email_normalization_strips_whitespace_and_lowercases(self):
        self.assertEqual(normalize_email("  NIDEESH@IITK.AC.IN "), "nideesh@iitk.ac.in")

    def test_user_creation_normalizes_email_and_hashes_password(self):
        with patch("logic.auth.authentication.password_hash.hash", return_value="hashed-password") as hash_password:
            user = create_user_record(
                self.db, "  NIDEESH@IITK.AC.IN ", "password123", "STUDENT"
            )

        self.assertEqual(user.email, "nideesh@iitk.ac.in")
        self.assertEqual(user.user_id, "nideesh")
        self.assertEqual(user.password_hash, "hashed-password")
        self.assertEqual(user.role, "STUDENT")
        hash_password.assert_called_once_with("password123")
        self.db.add.assert_called_once_with(user)
        self.db.flush.assert_called_once()
        self.db.commit.assert_not_called()

    def test_user_creation_rejects_non_iitk_email(self):
        with self.assertRaises(BadRequestError):
            create_user_record(self.db, "student@example.com", "password123", "STUDENT")

        self.db.query.assert_not_called()

    def test_user_creation_rejects_duplicate_email(self):
        self.db.query.return_value.filter.return_value.first.return_value = object()

        with self.assertRaises(ConflictError):
            create_user_record(self.db, "student@iitk.ac.in", "password123", "STUDENT")

        self.db.add.assert_not_called()
        self.db.flush.assert_not_called()

    def test_user_creation_rejects_duplicate_user_id(self):
        first = self.db.query.return_value.filter.return_value.first
        first.side_effect = [None, object()]

        with self.assertRaises(ConflictError):
            create_user_record(self.db, "student@iitk.ac.in", "password123", "STUDENT")

        self.assertEqual(first.call_count, 2)
        self.db.add.assert_not_called()

    def test_self_registration_rejects_professor_and_admin_roles(self):
        for role in ("PROFESSOR", "ADMIN"):
            with self.subTest(role=role):
                with self.assertRaises(ForbiddenError):
                    register_user(self.db, "staff@iitk.ac.in", "password123", role)

        self.db.query.assert_not_called()
        self.db.commit.assert_not_called()

    def test_self_registration_normalizes_role_and_commits_user(self):
        with patch("logic.auth.authentication.password_hash.hash", return_value="hashed-password"):
            user = register_user(
                self.db, "student@iitk.ac.in", "password123", "student"
            )

        self.assertIsInstance(user, User)
        self.assertEqual(user.role, "STUDENT")
        self.db.commit.assert_called_once()
        self.db.refresh.assert_called_once_with(user)

    def test_authentication_normalizes_email_before_lookup(self):
        user = User(
            user_id="student",
            email="student@iitk.ac.in",
            password_hash="stored-hash",
            role="STUDENT",
            failed_logins=0,
        )
        self.db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = user

        with patch("logic.auth.authentication.password_hash.verify", return_value=True) as verify:
            result = authenticate_user(self.db, " STUDENT@IITK.AC.IN ", "password123")

        self.assertIs(result, user)
        verify.assert_called_once_with("password123", "stored-hash")

    def test_authentication_returns_none_for_wrong_password(self):
        user = User(
            user_id="student",
            email="student@iitk.ac.in",
            password_hash="stored-hash",
            role="STUDENT",
            failed_logins=0,
        )
        self.db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = user

        with patch("logic.auth.authentication.password_hash.verify", return_value=False):
            result = authenticate_user(self.db, "student@iitk.ac.in", "wrong-password")

        self.assertIsNone(result)

    def test_authentication_returns_none_for_unknown_email(self):
        with patch("logic.auth.authentication.password_hash.verify") as verify:
            result = authenticate_user(self.db, "unknown@iitk.ac.in", "password123")

        self.assertIsNone(result)
        # A hash is always verified (against a dummy) so unknown accounts take as long as wrong passwords.
        verify.assert_called_once()
        self.assertNotEqual(verify.call_args.args[1], "stored-hash")


if __name__ == "__main__":
    unittest.main()
