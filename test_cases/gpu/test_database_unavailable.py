"""When PostgreSQL is unreachable the API answers 503 with Retry-After, not a bare 500
(Deliverable 3 failure drill F3), and does not leak the driver's connection details."""
import unittest

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError

from database import get_db
from main import app


def _down(exc):
    def dependency():
        raise exc
        yield  # pragma: no cover
    return dependency


class DatabaseUnavailableTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)

    def check(self, exc):
        app.dependency_overrides[get_db] = _down(exc)
        r = TestClient(app, raise_server_exceptions=False).post(
            "/auth/login", json={"email": "a@iitk.ac.in", "password": "whatever-123", "role": "STUDENT"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers["Retry-After"], "5")
        self.assertNotIn("55432", r.text)
        self.assertNotIn("refused", r.text)

    def test_connection_refused_is_503(self):
        self.check(OperationalError("SELECT 1", {}, Exception('connection to "127.0.0.1", port 55432 refused')))

    def test_pool_timeout_is_503(self):
        self.check(PoolTimeoutError("QueuePool limit reached; connection refused"))
