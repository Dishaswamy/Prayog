import pytest

from conftest import log_in_as

CREATED = {"project_id": "7f3c1e2a-0b1d-4c55-9d3e-123456789abc", "user_id": 1, "name": "vision",
           "description": None, "created_at": "2026-10-19T00:00:00", "updated_at": "2026-10-19T00:00:00"}


def test_create_sends_name_and_description_and_prints_the_project_id(run, server, capsys):
    log_in_as()
    server.on("POST", "/mlflow/projects", 201, CREATED)
    assert run("project", "create", "vision", "-d", "image models") == 0
    assert server.seen[0].headers["authorization"] == "Bearer tok-123"
    assert server.body() == {"name": "vision", "description": "image models"}
    out = capsys.readouterr().out
    assert "Created project 'vision'" in out and CREATED["project_id"] in out


def test_create_trims_the_name(run, server):
    log_in_as()
    server.on("POST", "/mlflow/projects", 201, CREATED)
    run("project", "create", "  vision  ")
    assert server.body()["name"] == "vision"


@pytest.mark.parametrize("name,fragment", [("   ", "cannot be blank"), ("x" * 121, "too long")])
def test_create_rejects_bad_names_without_calling_the_server(run, server, capsys, name, fragment):
    log_in_as()
    assert run("project", "create", name) == 2
    assert fragment in capsys.readouterr().err
    assert server.seen == []


def test_duplicate_name_shows_the_servers_reason(run, server, capsys):
    log_in_as()
    server.on("POST", "/mlflow/projects", 409, {"detail": "You already have a project named 'vision'"})
    assert run("project", "create", "vision") == 1
    assert "You already have a project named 'vision'" in capsys.readouterr().err


def test_logged_out_it_is_just_the_login_hint(run, server, capsys):
    assert run("project", "create", "vision") == 1
    assert capsys.readouterr().err == "Error: You are not logged in. Run `prayog login`.\n"
    assert server.seen == []


@pytest.mark.parametrize("role", ["STUDENT", "RESEARCHER", "PROFESSOR"])
def test_project_commands_are_listed_for_users_who_do_work(run, capsys, role):
    log_in_as(role)
    assert run("help") == 0
    assert "project" in capsys.readouterr().out


def test_admin_does_not_get_project_commands(run, server, capsys):
    log_in_as("ADMIN", "admin@iitk.ac.in")
    assert run("project", "create", "x") == 1
    assert "not available for your role (ADMIN)" in capsys.readouterr().err
    assert server.seen == []


@pytest.mark.parametrize("body,fragment", [
    ({"detail": "You do not have approved access yet. Request access in the Prayog web portal.", "code": "INSUFFICIENT_PERMISSION"},
     "do not have approved access yet"),
    ({"detail": "x", "code": "ACCESS_EXPIRED"}, "approved access period has ended"),
    ({"detail": "x", "code": "ACCESS_REVOKED"}, "access has been revoked"),
])
def test_no_access_replies_are_explained(run, server, capsys, body, fragment):
    log_in_as()
    server.on("POST", "/mlflow/projects", 403, body)
    assert run("project", "create", "vision") == 1
    assert fragment in capsys.readouterr().err
