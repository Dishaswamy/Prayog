from types import SimpleNamespace

from prayog.commands import review


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.response


def make_context(response):
    client = FakeClient(response)
    ctx = SimpleNamespace(client=lambda: client)
    return ctx, client


def test_list_requests_when_no_requests_exist(capsys):
    ctx, client = make_context([])

    review.list_requests(ctx, SimpleNamespace(status=None))

    assert client.calls == [("/access-requests", None)]
    assert "No requests." in capsys.readouterr().out


def test_show_request_prints_all_request_details(capsys):
    request = {
        "id": 42,
        "name": "Test User",
        "role": "STUDENT",
        "roll_number": "240001",
        "course": "CSE",
        "branch": "BTech",
        "purpose": "Research project",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "status_professor": "APPROVED",
        "status_admin": "APPROVED",
        "final_status": "APPROVED",
        "revoke_requested_at": None,
    }
    ctx, client = make_context(request)

    review.show(ctx, SimpleNamespace(id=42))

    output = capsys.readouterr().out

    assert client.calls == [("/access-requests/42", None)]
    assert "Request:" in output
    assert "42" in output
    assert "Test User" in output
    assert "240001" in output
    assert "CSE / BTech" in output
    assert "Research project" in output
    assert "2026-01-01" in output
    assert "2026-12-31" in output
    assert "APPROVED" in output
    assert "ACTIVE until 2026-12-31" in output