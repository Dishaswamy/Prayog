import stat

import pytest

from prayog import credentials
from conftest import log_in_as

ME = {"user_id": "dishac22", "email": "dishac22@iitk.ac.in", "role": "STUDENT"}


def stub_login(server, me=ME, requests=()):
    server.on("POST", "/auth/cli/login", body={"access_token": "tok-123", "token_type": "bearer"})
    server.on("GET", "/auth/me", body=me)
    server.on("GET", "/access-requests/me", body=list(requests))


def test_login_saves_token_but_never_the_password(run, server, home, monkeypatch, capsys):
    stub_login(server)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "s3cret-pass")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run("login", "--email", "dishac22@iitk.ac.in", "--role", "student") == 0

    assert server.body(0) == {"email": "dishac22@iitk.ac.in", "password": "s3cret-pass", "role": "STUDENT"}
    saved = credentials.load()
    assert saved.token == "tok-123" and saved.role == "STUDENT"
    assert "s3cret-pass" not in credentials.credentials_path().read_text()
    assert "Logged in as dishac22@iitk.ac.in" in capsys.readouterr().out


def test_credential_file_is_private(run, server, monkeypatch):
    stub_login(server)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-12345")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    run("login", "-e", "dishac22@iitk.ac.in", "-r", "student")
    path = credentials.credentials_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_login_reports_access_state_without_failing(run, server, monkeypatch, capsys):
    stub_login(server, requests=[{
        "id": 1, "final_status": "REVOKED", "revoke_reason": "EXPIRED", "revoked_at": "2027-05-01T00:00:00",
        "status_professor": "APPROVED", "status_admin": "APPROVED", "end_date": "2027-05-01T00:00:00",
        "revoke_requested_at": None}])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-12345")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run("login", "-e", "dishac22@iitk.ac.in", "-r", "student") == 0       # authenticated...
    assert "EXPIRED on 2027-05-01" in capsys.readouterr().out         # ...but the CLI says access ended


def test_wrong_password_is_invalid_credential_and_saves_nothing(run, server, monkeypatch, capsys):
    server.on("POST", "/auth/cli/login", 401, {"detail": "Invalid email, password or role"})
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "nope-nope")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run("login", "-e", "dishac22@iitk.ac.in", "-r", "student") == 1
    assert "Invalid email, password or role" in capsys.readouterr().err
    assert credentials.load() is None


def test_password_stdin(run, server, monkeypatch):
    stub_login(server)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("piped-pass\n"))
    assert run("login", "-e", "dishac22@iitk.ac.in", "-r", "student", "--password-stdin") == 0
    assert server.body(0)["password"] == "piped-pass"


def test_no_terminal_and_no_password_stdin_fails(run, server, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    assert run("login", "-e", "dishac22@iitk.ac.in", "-r", "student") == 2
    assert server.seen == []


def test_login_refuses_plain_http_to_remote_server(run, server, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("pw-12345\n"))
    code = run("login", "-e", "a@iitk.ac.in", "-r", "student", "--api-url", "http://prayog.iitk.ac.in", "--password-stdin")
    assert code == 2 and server.seen == []
    assert "https" in capsys.readouterr().err


def test_logout_ends_the_server_session_and_removes_the_local_login(run, server, capsys):
    log_in_as()
    server.on("POST", "/auth/logout", body={"message": "Logged out."})
    assert run("logout") == 0
    assert server.seen[0].headers["authorization"] == "Bearer tok-123"
    assert credentials.load() is None
    assert "stays installed" in capsys.readouterr().out


def test_logout_when_never_logged_in_does_not_call_the_server(run, server, capsys):
    assert run("logout") == 1
    assert "Run `prayog login`" in capsys.readouterr().err
    assert server.seen == []


def test_logout_still_clears_the_local_token_if_the_server_refuses_or_is_down(run, server, capsys):
    log_in_as()
    server.on("POST", "/auth/logout", 500, {"detail": "boom"})
    assert run("logout") == 0
    assert credentials.load() is None
    captured = capsys.readouterr()
    assert "stays installed" in captured.out
    assert "may stay valid" in captured.err


def test_logout_with_a_token_the_server_already_rejects_is_quiet(run, server, capsys):
    log_in_as()
    server.on("POST", "/auth/logout", 401, {"detail": "Logged out, please log in again"})
    assert run("logout") == 0
    assert credentials.load() is None
    assert capsys.readouterr().err == ""


def test_after_logout_protected_commands_ask_for_login(run, server, capsys):
    log_in_as()
    server.on("POST", "/auth/logout", body={"message": "Logged out."})
    run("logout")
    server.seen.clear()
    assert run("whoami") == 1
    assert "prayog login" in capsys.readouterr().err
    assert server.seen == []


def test_corrupt_credential_file_counts_as_logged_out(run, home, capsys):
    home.mkdir()
    (home / "credentials.json").write_text("{not json")
    assert run("whoami") == 1
    assert "prayog login" in capsys.readouterr().err


def test_login_requires_an_email_not_a_user_id(run, server, capsys):
    assert run("login", "-e", "dishac22", "-r", "student") == 2
    assert "institute email" in capsys.readouterr().err
    assert server.seen == []


def test_logging_in_again_ends_the_previous_cli_session_on_the_server(run, server, monkeypatch):
    log_in_as(email="old@iitk.ac.in")                       # saved token: tok-123
    stub_login(server)
    server.on("POST", "/auth/logout", body={"message": "Logged out."})
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-12345")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run("login", "-e", "dishac22@iitk.ac.in", "-r", "student") == 0

    logout_calls = [r for r in server.seen if r.url.path == "/auth/logout"]
    assert len(logout_calls) == 1 and logout_calls[0].headers["authorization"] == "Bearer tok-123"
    assert credentials.load().email == "dishac22@iitk.ac.in"


def test_failed_login_leaves_the_existing_session_alone(run, server, monkeypatch):
    log_in_as()
    server.on("POST", "/auth/cli/login", 401, {"detail": "Invalid email, password or role"})
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "nope-nope")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run("login", "-e", "dishac22@iitk.ac.in", "-r", "student") == 1
    assert credentials.load() is not None
    assert not [r for r in server.seen if r.url.path == "/auth/logout"]


@pytest.mark.parametrize("body,fragment", [
    ({"detail": "You do not have approved access yet. Request access in the Prayog web portal.", "code": "INSUFFICIENT_PERMISSION"},
     "do not have approved access yet"),
    ({"detail": "Your access request is still waiting for approval.", "code": "INSUFFICIENT_PERMISSION"},
     "still waiting for approval"),
    ({"detail": "x", "code": "ACCESS_EXPIRED"}, "approved access period has ended"),
    ({"detail": "x", "code": "ACCESS_REVOKED"}, "access has been revoked"),
])
def test_a_user_without_access_cannot_log_in_and_is_told_why(run, server, monkeypatch, capsys, body, fragment):
    server.on("POST", "/auth/cli/login", 403, body)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-12345")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run("login", "-e", "dishac22@iitk.ac.in", "-r", "student") == 1
    assert fragment in capsys.readouterr().err
    assert credentials.load() is None                       # nothing saved: they stay logged out
    assert [r.url.path for r in server.seen] == ["/auth/cli/login"]


def test_a_refused_login_keeps_the_previous_session_if_there_was_one(run, server, monkeypatch):
    log_in_as()
    server.on("POST", "/auth/cli/login", 403, {"detail": "x", "code": "ACCESS_REVOKED"})
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-12345")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run("login", "-e", "dishac22@iitk.ac.in", "-r", "student") == 1
    assert credentials.load().token == "tok-123"
