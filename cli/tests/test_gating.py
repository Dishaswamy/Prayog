"""The command surface: logged out it is exactly `login` and `help`, and nothing else is revealed."""
import pytest

from conftest import log_in_as

HIDDEN_NAMES = ("whoami", "access", "requests", "logout", "approve", "revoke", "--version")

LOGGED_OUT_DENIED = [
    ["whoami"], ["logout"], ["--version"], ["nonsense"],
    ["access"], ["access", "status"], ["access", "status", "--help"],
    ["requests"], ["requests", "list"], ["requests", "revoke", "7"],
    ["whoami", "--help"], ["help", "whoami"], ["help", "requests"], ["help", "nonsense"],
]


@pytest.mark.parametrize("argv", LOGGED_OUT_DENIED)
def test_logged_out_everything_but_login_and_help_gets_one_identical_answer(run, server, capsys, argv):
    assert run(*argv) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "Error: You are not logged in. Run `prayog login`.\n"      # same text: nothing leaks
    assert server.seen == []


@pytest.mark.parametrize("argv", [[], ["help"], ["-h"], ["--help"]])
def test_logged_out_help_lists_only_login_and_help(run, capsys, argv):
    if argv in (["-h"], ["--help"]):
        with pytest.raises(SystemExit) as exit_:
            run(*argv)
        assert exit_.value.code == 0
    else:
        assert run(*argv) == 0
    out = capsys.readouterr().out
    assert "login" in out and "help" in out
    commands_block = out.split("<command>", 2)[-1].split("options:")[0]
    for hidden in HIDDEN_NAMES:
        assert hidden not in commands_block, hidden


def test_logged_out_help_for_login_works(run, capsys):
    assert run("help", "login") == 0
    assert "--email" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run("login", "--help")
    assert "--password-stdin" in capsys.readouterr().out


def test_logged_out_login_with_a_bad_option_does_not_leak_other_commands(run, capsys):
    with pytest.raises(SystemExit) as exit_:
        run("login", "--bogus")
    assert exit_.value.code == 2
    err = capsys.readouterr().err
    assert not any(name in err for name in ("whoami", "requests", "access"))


def test_student_sees_student_commands_only(run, capsys):
    log_in_as("STUDENT")
    assert run("help") == 0
    out = capsys.readouterr().out
    for shown in ("whoami", "access", "logout", "login", "help"):
        assert shown in out
    assert "requests" not in out
    with pytest.raises(SystemExit) as exit_:
        run("--version")
    assert exit_.value.code == 0
    assert "prayog 0.1.0" in capsys.readouterr().out


def test_professor_sees_review_commands_not_access(run, capsys):
    log_in_as("PROFESSOR", "prof@iitk.ac.in")
    assert run("help") == 0
    out = capsys.readouterr().out
    assert "requests" in out and "\n    access " not in out


def test_logged_in_help_for_one_command(run, capsys):
    log_in_as("ADMIN", "admin@iitk.ac.in")
    assert run("help", "requests") == 0
    assert "revoke" in capsys.readouterr().out


def test_command_for_another_role_says_so_without_calling_the_server(run, server, capsys):
    log_in_as("STUDENT")
    assert run("requests", "list") == 1
    assert "not available for your role (STUDENT)" in capsys.readouterr().err
    assert run("help", "requests") == 1
    assert server.seen == []


def test_unknown_command_when_logged_in_is_a_usage_error(run, capsys):
    log_in_as()
    assert run("nonsense") == 2
    assert "Unknown command 'nonsense'" in capsys.readouterr().err


def test_there_is_no_professors_command(run, capsys):
    log_in_as()
    assert run("professors") == 2
    assert "Unknown command 'professors'" in capsys.readouterr().err
