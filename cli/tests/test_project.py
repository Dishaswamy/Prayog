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


def test_create_accepts_name_at_maximum_length(run, server):
    log_in_as()
    server.on("POST", "/mlflow/projects", 201, CREATED)

    name = "x" * 120
    assert run("project", "create", name) == 0
    assert server.body()["name"] == name


def test_create_accepts_description_at_maximum_length(run, server):
    log_in_as()
    server.on("POST", "/mlflow/projects", 201, CREATED)

    description = "d" * 4000
    assert run("project", "create", "vision", "-d", description) == 0
    assert server.body()["description"] == description


def test_create_rejects_description_over_maximum_length(
    run, server, capsys
):
    log_in_as()

    description = "d" * 4001

    assert run(
        "project", "create", "vision", "-d", description
    ) == 2

    assert "Description is too long" in capsys.readouterr().err
    assert server.seen == []


def test_create_accepts_empty_description(run, server):
    log_in_as()
    server.on("POST", "/mlflow/projects", 201, CREATED)

    assert run("project", "create", "vision", "-d", "") == 0
    assert server.body()["description"] == ""


def test_clean_deletes_staged_files(run, server, capsys):
    log_in_as()

    server.on("GET", "/mlflow/projects", body=[CREATED])
    server.on(
        "DELETE",
        f"/mlflow/projects/{CREATED['project_id']}/staged-data",
        200,
        {
            "deleted_files": 5,
            "freed_bytes": 2048,
            "kept_in_use": 0
        }
    )

    assert run("project", "clean", "vision") == 0

    out = capsys.readouterr().out
    assert "Deleted 5 staged file(s)" in out
    assert "2.0 KB" in out
    assert "Kept" not in out


def test_clean_reports_files_still_in_use(run, server, capsys):
    log_in_as()

    server.on("GET", "/mlflow/projects", body=[CREATED])
    server.on(
        "DELETE",
        f"/mlflow/projects/{CREATED['project_id']}/staged-data",
        200,
        {
            "deleted_files": 3,
            "freed_bytes": 1024,
            "kept_in_use": 2
        }
    )

    assert run("project", "clean", "vision") == 0

    out = capsys.readouterr().out
    assert "Deleted 3 staged file(s)" in out
    assert "Kept 2 file(s)" in out


def test_clean_handles_server_failure(run, server, capsys):
    log_in_as()

    server.on("GET", "/mlflow/projects", body=[CREATED])
    server.on(
        "DELETE",
        f"/mlflow/projects/{CREATED['project_id']}/staged-data",
        500,
        {"detail": "Internal server error"}
    )

    assert run("project", "clean", "vision") == 1
    assert "Internal server error" in capsys.readouterr().err
