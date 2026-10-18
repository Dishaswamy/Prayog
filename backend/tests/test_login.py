"""Login takes email or user ID, the password and a role; every failure looks the same."""
import unittest

from pydantic import ValidationError

from logic.admin import create_professor
from logic.auth import authenticate_user, generate_random_password, password_hash
from schemas.auth import UserLogin
from tests.scheduler_pg import SchedulerPostgresCase, requires_postgres

PASSWORD = "CorrectPassword123"


class LoginSchemaTests(unittest.TestCase):
    def test_role_is_required(self):
        with self.assertRaises(ValidationError):
            UserLogin(email="a@iitk.ac.in", password=PASSWORD)

    def test_role_must_be_a_known_one(self):
        with self.assertRaises(ValidationError):
            UserLogin(email="a@iitk.ac.in", password=PASSWORD, role="JANITOR")

    def test_role_is_case_insensitive(self):
        self.assertEqual(UserLogin(email="a@iitk.ac.in", password=PASSWORD, role="student").role, "STUDENT")

    def test_identifier_can_be_sent_as_email_user_id_or_identifier(self):
        for key in ("email", "user_id", "identifier"):
            body = UserLogin(**{key: "alice", "password": PASSWORD, "role": "STUDENT"})
            self.assertEqual(body.email, "alice")


class RandomPasswordTests(unittest.TestCase):
    def test_random_passwords_are_long_and_different(self):
        a, b = generate_random_password(), generate_random_password()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 32)


@requires_postgres
class LoginTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.student = self.make_user("login", role="STUDENT", access=False)
        self.student.password_hash = password_hash.hash(PASSWORD)
        self.db.commit()

    def test_login_with_email_and_role(self):
        self.assertEqual(authenticate_user(self.db, self.student.email, PASSWORD, "STUDENT").id, self.student.id)

    def test_login_with_user_id_and_role(self):
        self.assertEqual(authenticate_user(self.db, self.student.user_id, PASSWORD, "STUDENT").id, self.student.id)

    def test_email_and_user_id_ignore_case_and_spaces(self):
        self.assertIsNotNone(authenticate_user(self.db, f"  {self.student.email.upper()} ", PASSWORD, "student"))
        self.assertIsNotNone(authenticate_user(self.db, self.student.user_id.upper(), PASSWORD, "STUDENT"))

    def test_wrong_role_is_refused(self):
        for role in ("RESEARCHER", "PROFESSOR", "ADMIN"):
            self.assertIsNone(authenticate_user(self.db, self.student.email, PASSWORD, role))

    def test_wrong_password_is_refused(self):
        self.assertIsNone(authenticate_user(self.db, self.student.email, "WrongPassword123", "STUDENT"))

    def test_unknown_account_is_refused(self):
        self.assertIsNone(authenticate_user(self.db, f"ghost-{self.suffix}@example.test", PASSWORD, "STUDENT"))
        self.assertIsNone(authenticate_user(self.db, f"ghost-{self.suffix}", PASSWORD, "STUDENT"))

    def test_new_professor_logs_in_directly_with_the_generated_password(self):
        email = f"newprof-{self.suffix}@iitk.ac.in"
        professor = create_professor(self.db, "Prof New", email, "CSE")
        self.assertGreaterEqual(len(professor.temporary_password), 32)
        user = authenticate_user(self.db, email, professor.temporary_password, "PROFESSOR")
        self.assertEqual(user.id, professor.user_id)
        self.assertIsNone(authenticate_user(self.db, email, professor.temporary_password, "STUDENT"))

    def test_professor_with_an_explicit_password_gets_no_temporary_one(self):
        email = f"setprof-{self.suffix}@iitk.ac.in"
        professor = create_professor(self.db, "Prof Set", email, "CSE", password="ChosenPassword123")
        self.assertIsNone(professor.temporary_password)
        self.assertIsNotNone(authenticate_user(self.db, email, "ChosenPassword123", "PROFESSOR"))


if __name__ == "__main__":
    unittest.main()
