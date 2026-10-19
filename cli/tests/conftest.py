import json

import httpx
import pytest

from prayog import credentials
from prayog.cli import main


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAYOG_CONFIG_DIR", str(tmp_path / "prayog-home"))
    monkeypatch.delenv("PRAYOG_API_URL", raising=False)
    return tmp_path / "prayog-home"


class FakeServer:
    """Routes (method, path) to a status/body; records every request it sees."""

    def __init__(self):
        self.routes = {}
        self.seen = []

    def on(self, method, path, status=200, body=None):
        self.routes[(method, path)] = (status, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        status, body = self.routes.get((request.method, request.url.path), (404, {"detail": "Not found"}))
        return httpx.Response(status, json=body)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def body(self, index=-1):
        return json.loads(self.seen[index].content)


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def run(server):
    def _run(*argv):
        return main(list(argv), transport=server.transport)
    return _run


def log_in_as(role="STUDENT", email="dishac22@iitk.ac.in"):
    credentials.save(credentials.Credentials(
        api_url="http://localhost:8000", token="tok-123", email=email,
        user_id=email.split("@")[0], role=role))
