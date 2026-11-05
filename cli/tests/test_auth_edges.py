
from io import StringIO
from types import SimpleNamespace

import pytest

from prayog import credentials
from prayog.commands import auth
from prayog.errors import CliError
from conftest import log_in_as


def direct_args(**overrides):
    values = {
        "api_url": None,
        "allow_insecure_http": False,
        "email": "test@iitk.ac.in",
        "role": "student",
        "password_stdin": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_login_rejects_invalid_role_directly(monkeypatch):
    monkeypatch.setattr(auth, "prompt", lambda label, value, flag: value)

    ctx = SimpleNamespace(transport=None, creds=None)

    with pytest.raises(CliError, match="Role must be one of"):
        auth.login(ctx, direct_args(role="invalid"))


def test_login_rejects_empty_password_from_stdin(monkeypatch):
    monkeypatch.setattr(auth, "prompt", lambda label, value, flag: value)
    monkeypatch.setattr("sys.stdin", StringIO("\n"))

    ctx = SimpleNamespace(transport=None, creds=None)

    with pytest.raises(CliError, match="Password is required"):
        auth.login(ctx, direct_args())


def test_login_rejects_server_returning_different_role(
    run, server, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", StringIO("test-password\n"))

    server.on(
        "POST",
        "/auth/cli/login",
        body={"access_token": "tok-123", "token_type": "bearer"},
    )
    server.on(
        "GET",
        "/auth/me",
        body={
            "user_id": "test",
            "email": "test@iitk.ac.in",
            "role": "PROFESSOR",
        },
    )

    assert run(
        "login",
        "-e", "test@iitk.ac.in",
        "-r", "student",
        "--password-stdin",
    ) == 1

    assert "different role" in capsys.readouterr().err
    assert credentials.load() is None


def test_login_continues_if_previous_session_logout_fails(
    run, server, monkeypatch, capsys
):
    log_in_as(email="old@iitk.ac.in")

    server.on(
        "POST",
        "/auth/cli/login",
        body={"access_token": "new-token", "token_type": "bearer"},
    )
    server.on(
        "GET",
        "/auth/me",
        body={
            "user_id": "newuser",
            "email": "new@iitk.ac.in",
            "role": "STUDENT",
        },
    )
    server.on("POST", "/auth/logout", 500, {"detail": "Logout failed"})
    server.on("GET", "/access-requests/me", body=[])

    monkeypatch.setattr("sys.stdin", StringIO("test-password\n"))

    assert run(
        "login",
        "-e", "new@iitk.ac.in",
        "-r", "student",
        "--password-stdin",
    ) == 0

    assert credentials.load().token == "new-token"
    assert "Logged in as new@iitk.ac.in" in capsys.readouterr().out


def test_login_continues_if_access_status_lookup_fails(
    run, server, monkeypatch, capsys
):
    server.on(
        "POST",
        "/auth/cli/login",
        body={"access_token": "tok-123", "token_type": "bearer"},
    )
    server.on(
        "GET",
        "/auth/me",
        body={
            "user_id": "test",
            "email": "test@iitk.ac.in",
            "role": "STUDENT",
        },
    )
    server.on(
        "GET",
        "/access-requests/me",
        500,
        {"detail": "Access service unavailable"},
    )

    monkeypatch.setattr("sys.stdin", StringIO("test-password\n"))

    assert run(
        "login",
        "-e", "test@iitk.ac.in",
        "-r", "student",
        "--password-stdin",
    ) == 0

    assert credentials.load().token == "tok-123"
    assert "Logged in as test@iitk.ac.in" in capsys.readouterr().out
