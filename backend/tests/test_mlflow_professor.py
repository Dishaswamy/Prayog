import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from logic.errors import ForbiddenError, NotFoundError
from logic.mlflow_professor import _related_project, _related_user_ids


class ProfessorMLFlowAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    def test_non_professor_is_rejected_before_database_lookup(self):
        user = SimpleNamespace(id=10, role="STUDENT")

        with self.assertRaises(ForbiddenError):
            _related_user_ids(self.db, user)

        self.db.query.assert_not_called()

    def test_professor_without_profile_is_not_authorized(self):
        professor_query = MagicMock()
        professor_query.filter.return_value.first.return_value = None
        self.db.query.return_value = professor_query

        with self.assertRaises(NotFoundError):
            _related_user_ids(self.db, SimpleNamespace(id=10, role="PROFESSOR"))

        self.assertEqual(self.db.query.call_count, 1)

    def test_related_user_ids_are_deduplicated_across_students_and_researchers(self):
        professor = SimpleNamespace(id=4, user_id=10)
        professor_query = MagicMock()
        professor_query.filter.return_value.first.return_value = professor

        student_query = MagicMock()
        student_query.filter.return_value.all.return_value = [
            SimpleNamespace(user_id=20),
            SimpleNamespace(user_id=21),
        ]
        researcher_query = MagicMock()
        researcher_query.filter.return_value.all.return_value = [
            SimpleNamespace(user_id=21),
            SimpleNamespace(user_id=22),
        ]
        self.db.query.side_effect = [professor_query, student_query, researcher_query]

        result = _related_user_ids(
            self.db, SimpleNamespace(id=10, role="PROFESSOR")
        )

        self.assertEqual(result, [20, 21, 22])
        self.assertEqual(self.db.query.call_count, 3)

    def test_unrelated_project_is_reported_as_not_found(self):
        project_query = MagicMock()
        project_query.filter.return_value.first.return_value = None
        self.db.query.return_value = project_query

        with self.assertRaisesRegex(NotFoundError, "Project not found"):
            _related_project(self.db, "unrelated-project", [20])

    def test_related_project_can_be_returned(self):
        project = SimpleNamespace(project_id="project-1", user_id=20)
        project_query = MagicMock()
        project_query.filter.return_value.first.return_value = project
        self.db.query.return_value = project_query

        result = _related_project(self.db, "project-1", [20])

        self.assertIs(result, project)


if __name__ == "__main__":
    unittest.main()
