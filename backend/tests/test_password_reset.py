"""Password reset by emailed OTP: request, verify, limits, and old sessions dying."""
import unittest
from datetime import timedelta

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

import models as orm
from logic.auth import (
    OTP_MAX_ATTEMPTS,
    authenticate_user,
    create_access_token,
    password_hash,
    request_password_reset_otp,
    verify_password_reset_otp,
)
from logic.dependencies import get_current_user
from logic.errors import BadRequestError
from schemas.auth import ForgotPasswordIn, VerifyOtpIn
from tests.scheduler_pg import SchedulerPostgresCase, requires_postgres

OLD, NEW = "OldPassword123", "NewPassword456"


class ResetSchemaTests(unittest.TestCase):
    def test_passwords_must_match(self):
        with self.assertRaises(ValidationError):
            ForgotPasswordIn(email="a@iitk.ac.in", new_password=NEW, confirm_password="different123")

    def test_new_password_must_be_long_enough(self):
        with self.assertRaises(ValidationError):
            ForgotPasswordIn(email="a@iitk.ac.in", new_password="short", confirm_password="short")

    def test_otp_must_be_six_digits(self):
        for bad in ("12345", "1234567", "12345a", ""):
            with self.assertRaises(ValidationError):
                VerifyOtpIn(email="a@iitk.ac.in", otp=bad)
        VerifyOtpIn(email="a@iitk.ac.in", otp="012345")


@requires_postgres
class PasswordResetTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("reset", access=False)
        self.user.password_hash = password_hash.hash(OLD)
        self.db.commit()

    def _request(self):
        return request_password_reset_otp(self.db, self.user.email, NEW)

    def _latest(self):
        return (
            self.db.query(orm.PasswordResetOTP)
            .filter_by(user_id=self.user.id).order_by(orm.PasswordResetOTP.id.desc()).first()
        )

    def _skip_cooldown(self):
        record = self._latest()
        record.created_at -= timedelta(minutes=5)
        self.db.commit()

    def test_request_returns_a_six_digit_otp_and_stores_only_hashes(self):
        email, otp = self._request()
        self.assertEqual(email, self.user.email)
        self.assertRegex(otp, r"^\d{6}$")
        record = self._latest()
        self.assertNotIn(otp, (record.otp_hash, record.new_password_hash))
        self.assertNotIn(NEW, record.new_password_hash)

    def test_unknown_email_sends_nothing(self):
        self.assertIsNone(request_password_reset_otp(self.db, f"nobody-{self.suffix}@example.test", NEW))

    def test_second_request_inside_the_cooldown_is_ignored(self):
        self.assertIsNotNone(self._request())
        self.assertIsNone(self._request())

    def test_correct_otp_changes_the_password(self):
        _, otp = self._request()
        verify_password_reset_otp(self.db, self.user.email, otp)
        self.assertIsNotNone(authenticate_user(self.db, self.user.email, NEW))
        self.assertIsNone(authenticate_user(self.db, self.user.email, OLD))

    def test_password_does_not_change_before_the_otp_is_verified(self):
        self._request()
        self.assertIsNotNone(authenticate_user(self.db, self.user.email, OLD))
        self.assertIsNone(authenticate_user(self.db, self.user.email, NEW))

    def test_otp_works_only_once(self):
        _, otp = self._request()
        verify_password_reset_otp(self.db, self.user.email, otp)
        with self.assertRaises(BadRequestError):
            verify_password_reset_otp(self.db, self.user.email, otp)

    def test_wrong_otp_is_refused_and_counted(self):
        _, otp = self._request()
        wrong = "000000" if otp != "000000" else "111111"
        with self.assertRaises(BadRequestError):
            verify_password_reset_otp(self.db, self.user.email, wrong)
        self.assertEqual(self._latest().attempts, 1)
        self.assertIsNotNone(authenticate_user(self.db, self.user.email, OLD))

    def test_too_many_wrong_attempts_cancel_the_otp_even_for_the_right_code(self):
        _, otp = self._request()
        wrong = "000000" if otp != "000000" else "111111"
        for _ in range(OTP_MAX_ATTEMPTS):
            with self.assertRaises(BadRequestError):
                verify_password_reset_otp(self.db, self.user.email, wrong)
        with self.assertRaises(BadRequestError):
            verify_password_reset_otp(self.db, self.user.email, otp)
        self.assertIsNotNone(authenticate_user(self.db, self.user.email, OLD))

    def test_expired_otp_is_refused(self):
        _, otp = self._request()
        record = self._latest()
        record.expires_at = record.created_at - timedelta(seconds=1)
        self.db.commit()
        with self.assertRaises(BadRequestError):
            verify_password_reset_otp(self.db, self.user.email, otp)

    def test_a_new_otp_cancels_the_older_one(self):
        _, first = self._request()
        self._skip_cooldown()
        _, second = self._request()
        if first != second:
            with self.assertRaises(BadRequestError):
                verify_password_reset_otp(self.db, self.user.email, first)
        verify_password_reset_otp(self.db, self.user.email, second)
        self.assertIsNotNone(authenticate_user(self.db, self.user.email, NEW))

    def test_unknown_email_cannot_verify(self):
        with self.assertRaises(BadRequestError):
            verify_password_reset_otp(self.db, f"nobody-{self.suffix}@example.test", "123456")

    def test_old_sessions_stop_working_after_a_reset(self):
        token = create_access_token(self.user)
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        self.assertEqual(get_current_user(creds, db=self.db).id, self.user.id)   # valid before

        _, otp = self._request()
        verify_password_reset_otp(self.db, self.user.email, otp)

        with self.assertRaises(HTTPException) as caught:
            get_current_user(creds, db=self.db)
        self.assertEqual(caught.exception.status_code, 401)

        self.db.refresh(self.user)
        fresh = HTTPAuthorizationCredentials(scheme="Bearer", credentials=create_access_token(self.user))
        self.assertEqual(get_current_user(fresh, db=self.db).id, self.user.id)   # a new login works


if __name__ == "__main__":
    unittest.main()
