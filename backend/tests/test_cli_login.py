"""POST /auth/cli/login: the terminal login only works for users who currently have access.
The web portal's POST /auth/login is unchanged, since a student must log in there to request access."""
from datetime import datetime, timedelta

from database import SessionLocal
from logic.access_request import sweep_access_revocations
from models import AccessRequest, Student
from tests.access_api_case import PW, AccessApiCase
from tests.scheduler_pg import requires_postgres


@requires_postgres
class CliLoginTests(AccessApiCase):
    def cli_login(self, label, role="STUDENT", password=PW):
        return self.client.post("/auth/cli/login", json={
            "email": f"{label}-{self.suffix}@iitk.ac.in", "password": password, "role": role})

    def assertRefused(self, response, code):
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], code)
        self.assertNotIn("access_token", response.json())

    def test_a_user_who_never_requested_access_cannot_log_in_from_the_terminal(self):
        self.new_user("nobody")
        self.assertRefused(self.cli_login("nobody"), "INSUFFICIENT_PERMISSION")

    def test_pending_and_rejected_requests_cannot_log_in(self):
        auth = self.new_user("waiting")
        request_id = self.request_access(auth, "waiting")
        r = self.cli_login("waiting")
        self.assertRefused(r, "INSUFFICIENT_PERMISSION")
        self.assertIn("waiting for approval", r.json()["detail"])
        self.decide(request_id, self.prof)                     # the professor alone is not enough
        self.assertRefused(self.cli_login("waiting"), "INSUFFICIENT_PERMISSION")
        self.decide(request_id, self.admin, "REJECTED")
        r = self.cli_login("waiting")
        self.assertRefused(r, "INSUFFICIENT_PERMISSION")
        self.assertIn("rejected", r.json()["detail"])

    def test_the_web_portal_login_still_works_for_a_user_without_access(self):
        self.new_user("portal")                                # new_user logs in through /auth/login
        self.assertRefused(self.cli_login("portal"), "INSUFFICIENT_PERMISSION")

    def test_approved_users_get_a_token_that_works(self):
        self.approved_user("stu")
        self.approved_user("res", "RESEARCHER")
        for label, role in (("stu", "STUDENT"), ("res", "RESEARCHER")):
            r = self.cli_login(label, role)
            self.assertEqual(r.status_code, 200, r.text)
            auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
            self.assertEqual(self.client.get("/auth/me", headers=auth).json()["role"], role)
            self.assertEqual(self.client.post("/mlflow/projects", headers=auth, json={"name": "p"}).status_code, 201)

    def test_revoked_user_cannot_log_in_again(self):
        _, request_id = self.approved_user("revoked")
        self.assertEqual(self.cli_login("revoked").status_code, 200)
        self.client.post(f"/access-requests/{request_id}/revoke", headers=self.admin)
        self.assertRefused(self.cli_login("revoked"), "ACCESS_REVOKED")

    def test_student_past_the_end_date_cannot_log_in_even_before_the_sweep(self):
        _, request_id = self.approved_user("lapsed")
        with SessionLocal() as db:
            user_id = db.get(AccessRequest, request_id).user_id
            db.query(Student).filter(Student.user_id == user_id).one().end_date = datetime.utcnow() - timedelta(minutes=1)
            db.commit()
        self.assertRefused(self.cli_login("lapsed"), "ACCESS_EXPIRED")

    def test_auto_revoked_student_is_expired_not_revoked(self):
        _, request_id = self.approved_user("swept")
        with SessionLocal() as db:
            db.get(AccessRequest, request_id).end_date = datetime.utcnow() - timedelta(minutes=1)
            db.commit()
            sweep_access_revocations(db)
        self.assertRefused(self.cli_login("swept"), "ACCESS_EXPIRED")

    def test_wrong_password_or_role_is_the_same_generic_401_and_reveals_nothing_about_access(self):
        self.approved_user("secret")
        self.new_user("noaccess")
        for r in (self.cli_login("secret", password="WrongPassword123"),
                  self.cli_login("secret", role="RESEARCHER"),
                  self.cli_login("noaccess", password="WrongPassword123"),
                  self.cli_login("ghost")):
            self.assertEqual((r.status_code, r.json()["detail"]), (401, "Invalid email, password or role"))
            self.assertNotIn("code", r.json())

    def test_professor_and_admin_can_log_in(self):
        for email, password, role in ((self.prof_email, self.prof_password, "PROFESSOR"), (self.admin_email, PW, "ADMIN")):
            r = self.client.post("/auth/cli/login", json={"email": email, "password": password, "role": role})
            self.assertEqual(r.status_code, 200, r.text)
