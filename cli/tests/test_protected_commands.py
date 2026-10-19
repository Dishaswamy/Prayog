import pytest

from conftest import log_in_as

REQ = {"id": 7, "user_id": 1, "name": "Disha", "role": "STUDENT", "course": "BTech", "branch": "AE",
       "purpose": "COURSE_PROJECT", "professor_id": 1, "roll_number": "220345",
       "start_date": "2026-10-15T00:00:00", "end_date": "2027-05-01T00:00:00",
       "status_professor": "PENDING", "status_admin": "PENDING", "final_status": "PENDING",
       "revoke_requested_at": None, "revoked_at": None, "revoke_reason": None}


def test_whoami(run, server, capsys):
    log_in_as()
    server.on("GET", "/auth/me", body={"user_id": "dishac22", "email": "dishac22@iitk.ac.in", "role": "STUDENT"})
    assert run("whoami") == 0
    assert server.seen[0].headers["authorization"] == "Bearer tok-123"
    assert "dishac22@iitk.ac.in" in capsys.readouterr().out


def test_status_translates_each_state(run, server, capsys):
    log_in_as()
    cases = {
        "approved": ({"final_status": "APPROVED"}, "ACTIVE until 2027-05-01"),
        "pending": ({}, "PENDING (professor: PENDING, admin: PENDING)"),
        "expired": ({"final_status": "REVOKED", "revoke_reason": "EXPIRED", "revoked_at": "2027-05-01T00:00:00"}, "EXPIRED on 2027-05-01"),
        "revoked": ({"final_status": "REVOKED", "revoke_reason": "ADMIN", "revoked_at": "2026-11-02T00:00:00"}, "REVOKED on 2026-11-02 by an admin"),
        "ending": ({"final_status": "APPROVED", "revoke_requested_at": "2026-11-02T00:00:00"}, "ENDING"),
    }
    for name, (patch, expected) in cases.items():
        server.on("GET", "/access-requests/me", body=[{**REQ, **patch}])
        assert run("access", "status") == 0, name
        assert expected in capsys.readouterr().out, name


def test_status_with_no_request(run, server, capsys):
    log_in_as()
    server.on("GET", "/access-requests/me", body=[])
    run("access", "status")
    assert "web portal" in capsys.readouterr().out


def test_requests_list_filters_and_decides(run, server, capsys):
    log_in_as("PROFESSOR", "prof@iitk.ac.in")
    server.on("GET", "/access-requests", body=[REQ])
    assert run("requests", "list", "--status", "pending") == 0
    assert server.seen[0].url.params["status"] == "PENDING"
    assert "Disha" in capsys.readouterr().out

    server.on("POST", "/access-requests/7/decision", body={**REQ, "status_professor": "APPROVED"})
    assert run("requests", "approve", "7") == 0
    assert server.body() == {"status": "APPROVED"}
    assert run("requests", "reject", "7") == 0
    assert server.body() == {"status": "REJECTED"}


def test_revoke(run, server, capsys):
    log_in_as("ADMIN", "admin@iitk.ac.in")
    server.on("POST", "/access-requests/7/revoke", body={**REQ, "final_status": "REVOKED", "revoke_reason": "ADMIN",
                                                         "revoked_at": "2026-11-02T00:00:00"})
    assert run("requests", "revoke", "7") == 0
    assert "REVOKED" in capsys.readouterr().out


@pytest.mark.parametrize("status,body,fragment", [
    (401, {"detail": "Token has expired"}, "session has expired. Run `prayog login`"),
    (401, {"detail": "Session expired, please log in again"}, "session has expired"),
    (401, {"detail": "Invalid authentication token"}, "did not accept your saved login"),
    (403, {"detail": "Requires role: ADMIN"}, "Requires role: ADMIN"),
    (403, {"detail": "x", "code": "ACCESS_EXPIRED"}, "approved access period has ended"),
    (403, {"detail": "x", "code": "ACCESS_REVOKED"}, "access has been revoked"),
    (403, {"detail": "No lab.", "code": "AUTHORIZATION_INACTIVE"}, "access must be approved first"),
    (409, {"detail": "This request is already approved and locked."}, "already approved and locked"),
    (422, {"detail": [{"loc": ["body", "status"], "msg": "bad value"}]}, "status: bad value"),
    (500, {"detail": "boom"}, "internal error"),
])
def test_server_denials_become_actionable_messages(run, server, capsys, status, body, fragment):
    log_in_as("ADMIN", "admin@iitk.ac.in")
    server.on("POST", "/access-requests/7/revoke", status, body)
    assert run("requests", "revoke", "7") == 1
    assert fragment in capsys.readouterr().err


def test_unreachable_server_fails_closed(run, capsys, monkeypatch):
    import httpx
    log_in_as()
    def boom(request):
        raise httpx.ConnectError("down")
    from prayog.cli import main
    assert main(["whoami"], transport=httpx.MockTransport(boom)) == 1
    assert "Could not reach the Prayog server" in capsys.readouterr().err


def test_access_request_points_to_the_web_portal_and_calls_nothing(run, server, capsys, monkeypatch):
    log_in_as()
    assert run("access", "request") == 0
    out = capsys.readouterr().out
    assert "web portal" in out and "prayog access status" in out
    monkeypatch.setenv("PRAYOG_PORTAL_URL", "https://prayog.iitk.ac.in")
    assert run("access", "request") == 0
    assert "https://prayog.iitk.ac.in" in capsys.readouterr().out
    assert server.seen == []


def test_professor_cannot_use_access_commands(run, server, capsys):
    log_in_as("PROFESSOR", "prof@iitk.ac.in")
    assert run("access", "status") == 1
    assert "not available for your role (PROFESSOR)" in capsys.readouterr().err
    assert server.seen == []
