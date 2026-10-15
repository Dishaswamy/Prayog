"""PostgreSQL-backed relationship isolation tests for Professor ML Flow.

These tests run against the migrated CI PostgreSQL database. Each test uses a
transaction that is rolled back, so it does not leave fixture rows behind.
"""
import os
import unittest
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from database import SessionLocal
from logic.mlflow_professor import _related_user_ids, list_projects
from models import Professor, Researcher, Student, User
from mlflow_models import Project


@unittest.skipUnless(
    os.getenv("PRAYOG_RUN_PROFESSOR_POSTGRES_TESTS") == "1",
    "requires the dedicated migrated PostgreSQL test database",
)
class ProfessorMLFlowPostgresIsolationTests(unittest.TestCase):
    def setUp(self):
        self.db: Session = SessionLocal()
        self.suffix = uuid4().hex[:12]
        self.now = datetime.utcnow()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def _user(self, label: str, role: str) -> User:
        user = User(
            user_id=f"{label}-{self.suffix}",
            email=f"{label}-{self.suffix}@example.test",
            password_hash="not-a-real-password-hash",
            role=role,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def _professor(self, user: User) -> Professor:
        professor = Professor(
            user_id=user.id,
            name="Test Professor",
            email=f"prof-{self.suffix}@example.test",
            department="Test Department",
        )
        self.db.add(professor)
        self.db.flush()
        return professor

    def _student(self, user: User, professor: Professor, end_date: datetime) -> Student:
        student = Student(
            user_id=user.id,
            name="Test Student",
            email=f"student-{user.id}-{self.suffix}@example.test",
            roll_number=f"roll-{user.id}-{self.suffix}",
            course="BTech",
            branch="CSE",
            professor_id=professor.id,
            start_date=self.now - timedelta(days=30),
            end_date=end_date,
        )
        self.db.add(student)
        self.db.flush()
        return student

    def _project(self, user: User, name: str) -> Project:
        project = Project(
            # The production schema limits project_id to 36 characters.
            project_id=str(uuid4()),
            user_id=user.id,
            name=name,
            description=None,
        )
        self.db.add(project)
        self.db.flush()
        return project

    def test_only_active_related_students_are_visible(self):
        professor_user = self._user("professor", "PROFESSOR")
        professor = self._professor(professor_user)
        active_student = self._user("active-student", "STUDENT")
        expired_student = self._user("expired-student", "STUDENT")
        future_student = self._user("future-student", "STUDENT")
        unrelated_student = self._user("unrelated-student", "STUDENT")

        self._student(active_student, professor, self.now + timedelta(days=1))
        self._student(expired_student, professor, self.now - timedelta(days=1))
        future_relationship = self._student(
            future_student, professor, self.now + timedelta(days=2)
        )
        future_relationship.start_date = self.now + timedelta(days=1)
        # No Student row for unrelated_student: no approved relationship.
        active_project = self._project(active_student, "active-project")
        self._project(expired_student, "expired-project")
        self._project(future_student, "future-project")
        self._project(unrelated_student, "unrelated-project")
        self.db.flush()

        visible_user_ids = _related_user_ids(self.db, professor_user)
        visible_projects = list_projects(self.db, professor_user)

        self.assertEqual(visible_user_ids, [active_student.id])
        self.assertEqual([project.project_id for project in visible_projects],
                         [active_project.project_id])

    def test_removed_relationship_row_revokes_visibility(self):
        professor_user = self._user("professor", "PROFESSOR")
        professor = self._professor(professor_user)
        formerly_related_user = self._user("former-student", "STUDENT")
        relationship = self._student(
            formerly_related_user, professor, self.now + timedelta(days=30)
        )
        self._project(formerly_related_user, "former-project")
        self.db.flush()

        # Model revocation exactly: access management removes the approved row.
        self.db.delete(relationship)
        self.db.flush()

        self.assertEqual(_related_user_ids(self.db, professor_user), [])
        self.assertEqual(list_projects(self.db, professor_user), [])

    def test_approved_researcher_is_visible_until_relationship_row_is_removed(self):
        professor_user = self._user("professor", "PROFESSOR")
        professor = self._professor(professor_user)
        researcher_user = self._user("researcher", "RESEARCHER")
        researcher = Researcher(
            user_id=researcher_user.id,
            name="Test Researcher",
            email=f"researcher-{self.suffix}@example.test",
            roll_number=f"researcher-roll-{self.suffix}",
            course="PhD",
            branch="CSE",
            professor_id=professor.id,
            start_date=self.now - timedelta(days=10),
        )
        future_researcher_user = self._user("future-researcher", "RESEARCHER")
        future_researcher = Researcher(
            user_id=future_researcher_user.id,
            name="Future Researcher",
            email=f"future-researcher-{self.suffix}@example.test",
            roll_number=f"future-researcher-roll-{self.suffix}",
            course="PhD",
            branch="CSE",
            professor_id=professor.id,
            start_date=self.now + timedelta(days=1),
        )
        self.db.add_all([researcher, future_researcher])
        self._project(researcher_user, "researcher-project")
        self._project(future_researcher_user, "future-researcher-project")
        self.db.flush()

        self.assertEqual(_related_user_ids(self.db, professor_user), [researcher_user.id])
        self.assertEqual(len(list_projects(self.db, professor_user)), 1)

        # Removing the approved researcher row revokes visibility immediately.
        self.db.delete(researcher)
        self.db.flush()
        self.assertEqual(_related_user_ids(self.db, professor_user), [])
        self.assertEqual(list_projects(self.db, professor_user), [])


if __name__ == "__main__":
    unittest.main()
