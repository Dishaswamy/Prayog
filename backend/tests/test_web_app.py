"""The web app at /app, and the two read routes it needs: the owner's jobs (FR-GPU-154) and the
handovers a Professor reviews (FR-ML-009)."""
import unittest

from fastapi.testclient import TestClient

import models as orm
from main import app
from tests.gpu_case import GpuRunCase, requires_postgres


class WebAppServingTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_the_root_goes_to_the_app(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (307, "/app/"))

    def test_the_app_is_served_under_a_strict_policy(self):
        response = self.client.get("/app/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_the_stewards_modules_are_served_even_when_it_is_not_running(self):
        for path in ("/app/main.js", "/app/web.css", "/app/steward/lib.js", "/app/steward/style.css"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_api_responses_keep_their_own_headers(self):
        self.assertNotIn("content-security-policy", self.client.get("/health").headers)


@requires_postgres
class MyJobsTests(GpuRunCase):
    def test_the_list_is_the_owners_jobs_newest_first(self):
        user = self.make_user("jobs-a")
        client = self.client_as(user)
        ids = [client.post("/jobs", json={"gpus": 1, "walltime": "00:30", "client_request_id": f"{self.suffix}-{i}"}).json()["request_id"]
               for i in range(3)]
        listed = client.get("/jobs").json()
        self.assertEqual([j["id"] for j in listed], ids[::-1])
        self.assertEqual(listed[0]["id"], client.get(f"/jobs/{ids[-1]}").json()["id"])
        self.assertEqual(len(client.get("/jobs", params={"limit": 2}).json()), 2)

        other = self.client_for_other(self.make_user("jobs-b"))
        self.assertEqual(other.get("/jobs").json(), [])

    def test_the_limit_is_bounded(self):
        client = self.client_as(self.make_user("jobs-c"))
        self.assertEqual(client.get("/jobs", params={"limit": 0}).status_code, 422)


@requires_postgres
class HandoverListTests(GpuRunCase):
    def setUp(self):
        super().setUp()
        self.owner = self.make_user("hand-a")
        self.client = self.client_as(self.owner)
        project = self.make_project(self.client)
        self.make_version(self.client, project)
        self.professor = self.db.query(orm.Professor).filter(orm.Professor.email == f"prof-{self.suffix}@example.test").one()
        pushed = self.client.post(f"/mlflow/projects/{project['project_id']}/handover", json={"professor_id": self.professor.id})
        self.assertEqual(pushed.status_code, 201, pushed.text)
        self.handover = pushed.json()["handover_id"]

    def ids(self, client, **params):
        return [h["handover_id"] for h in client.get("/mlflow/handovers", params=params).json()]

    def test_the_owner_sees_their_request(self):
        self.assertEqual(self.ids(self.client), [self.handover])

    def test_the_named_professor_sees_it_and_can_filter(self):
        client = self.client_for_other(self.db.get(orm.User, self.professor.user_id))
        self.assertEqual(self.ids(client, status="HANDOVER_PENDING"), [self.handover])
        self.assertEqual(self.ids(client, status="HANDOVER_APPROVED"), [])

    def test_nobody_else_sees_it(self):
        self.assertEqual(self.ids(self.client_for_other(self.make_user("hand-b"))), [])
        other = orm.User(user_id=f"p2-{self.suffix}"[:50], email=f"p2-{self.suffix}@example.test",
                         password_hash="not-a-real-password-hash", role="PROFESSOR")
        self.db.add(other)
        self.db.flush()
        self.db.add(orm.Professor(user_id=other.id, name="Other", email=other.email, department="CSE"))
        self.db.commit()
        self.assertEqual(self.ids(self.client_for_other(other)), [])

    def test_an_unknown_status_is_refused(self):
        self.assertEqual(self.client.get("/mlflow/handovers", params={"status": "NOPE"}).status_code, 422)
