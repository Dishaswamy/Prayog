"""The auth routes end to end: signup, login with a role, password reset, new professors."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from database import SessionLocal
from logic.auth import create_user_record
from main import app
from tests.scheduler_pg import SchedulerPostgresCase, requires_postgres

OLD, NEW = "OldPassword123", "NewPassword456"


@requires_postgres
class AuthRouteTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self.email = f"route-{self.suffix}@iitk.ac.in"
        self.user_id = self.email.split("@")[0]

    def _register(self, role="STUDENT"):
        r = self.client.post("/auth/register", json={"email": self.email, "password": OLD, "role": role})
        self.assertEqual(r.status_code, 201, r.text)

    def _login(self, identifier, password, role):
        return self.client.post("/auth/login", json={"email": identifier, "password": password, "role": role})

    def test_signup_then_login_by_email_or_user_id_with_the_role(self):
        self._register("STUDENT")
        for identifier in (self.email, self.user_id, self.email.upper()):
            r = self._login(identifier, OLD, "STUDENT")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("access_token", r.json())

    def test_wrong_role_wrong_password_and_unknown_user_look_identical(self):
        self._register("STUDENT")
        replies = [
            self._login(self.email, OLD, "RESEARCHER"),
            self._login(self.email, "WrongPassword123", "STUDENT"),
            self._login(f"ghost-{self.suffix}@iitk.ac.in", OLD, "STUDENT"),
        ]
        for r in replies:
            self.assertEqual((r.status_code, r.json()["detail"]), (401, "Invalid email, password or role"))

    def test_login_without_a_role_is_rejected(self):
        self._register()
        r = self.client.post("/auth/login", json={"email": self.email, "password": OLD})
        self.assertEqual(r.status_code, 422)

    def test_full_password_reset_by_otp(self):
        self._register()
        old_token = self._login(self.email, OLD, "STUDENT").json()["access_token"]
        old_auth = {"Authorization": f"Bearer {old_token}"}
        self.assertEqual(self.client.get("/auth/me", headers=old_auth).status_code, 200)

        sent = []
        with patch("routers.auth.send_password_reset_otp_email", lambda *a: sent.append(a)):
            r = self.client.post("/auth/forgot-password", json={
                "email": self.email, "new_password": NEW, "confirm_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        (to, otp, minutes), = sent
        self.assertEqual((to, minutes), (self.email, 10))

        # nothing has changed until the OTP is submitted
        self.assertEqual(self._login(self.email, OLD, "STUDENT").status_code, 200)
        self.assertEqual(self._login(self.email, NEW, "STUDENT").status_code, 401)

        wrong = "000000" if otp != "000000" else "111111"
        r = self.client.post("/auth/verify-otp", json={"email": self.email, "otp": wrong})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "Invalid or expired OTP"))

        r = self.client.post("/auth/verify-otp", json={"email": self.email, "otp": otp})
        self.assertEqual(r.status_code, 200, r.text)

        self.assertEqual(self._login(self.email, NEW, "STUDENT").status_code, 200)
        self.assertEqual(self._login(self.email, OLD, "STUDENT").status_code, 401)
        # the session from before the reset is dead
        r = self.client.get("/auth/me", headers=old_auth)
        self.assertEqual((r.status_code, r.json()["detail"]), (401, "Session expired, please log in again"))
        # and the OTP cannot be used twice
        self.assertEqual(self.client.post("/auth/verify-otp", json={"email": self.email, "otp": otp}).status_code, 400)

    def test_forgot_password_reply_is_the_same_for_unknown_emails_and_sends_nothing(self):
        sent = []
        with patch("routers.auth.send_password_reset_otp_email", lambda *a: sent.append(a)):
            r = self.client.post("/auth/forgot-password", json={
                "email": f"ghost-{self.suffix}@iitk.ac.in", "new_password": NEW})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sent, [])

    def test_forgot_password_rejects_mismatched_or_short_passwords(self):
        for body in (
            {"email": self.email, "new_password": NEW, "confirm_password": "Different123"},
            {"email": self.email, "new_password": "short"},
        ):
            self.assertEqual(self.client.post("/auth/forgot-password", json=body).status_code, 422)

    def test_admin_creates_a_professor_who_logs_in_with_the_given_password(self):
        admin_email = f"adm-{self.suffix}@iitk.ac.in"
        with SessionLocal() as db:
            create_user_record(db, admin_email, OLD, "ADMIN")
            db.commit()
        token = self._login(admin_email, OLD, "ADMIN").json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        prof_email = f"prof-{self.suffix}@iitk.ac.in"
        r = self.client.post("/professors", headers=auth, json={
            "name": "Prof Route", "email": prof_email, "department": "CSE", "lab_id": self.lab.id})
        self.assertEqual(r.status_code, 201, r.text)
        temporary = r.json()["temporary_password"]
        self.assertGreaterEqual(len(temporary), 32)
        self.assertEqual(r.json()["lab_id"], self.lab.id)

        self.assertEqual(self._login(prof_email, temporary, "PROFESSOR").status_code, 200)   # directly
        self.assertEqual(self._login(prof_email, temporary, "STUDENT").status_code, 401)

        # the password is shown once: the list never includes it
        listing = self.client.get("/professors", headers=auth).json()
        self.assertTrue(all("temporary_password" not in p for p in listing))
