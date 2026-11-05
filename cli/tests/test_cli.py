
from argparse import Namespace

import pytest

from prayog import cli


def test_main_defence_in_depth_rejects_protected_command_without_credentials(
    monkeypatch, capsys
):
    class FakeParser:
        def parse_args(self, argv):
            return Namespace(command="whoami", func=lambda ctx, args: None)

    monkeypatch.setattr(cli, "_gate", lambda argv, creds: None)
    monkeypatch.setattr(cli, "build_parser", lambda creds: FakeParser())

    assert cli.main(["whoami"]) == 1
    assert "You are not logged in" in capsys.readouterr().err


def test_main_handles_keyboard_interrupt(monkeypatch, capsys):
    class FakeParser:
        def parse_args(self, argv):
            def interrupt(ctx, args):
                raise KeyboardInterrupt

            return Namespace(command="login", func=interrupt)

    monkeypatch.setattr(cli, "_gate", lambda argv, creds: None)
    monkeypatch.setattr(cli, "build_parser", lambda creds: FakeParser())

    assert cli.main(["login"]) == 130
    assert "Cancelled." in capsys.readouterr().err


def test_entrypoint_exits_with_main_return_code(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: 7)

    with pytest.raises(SystemExit) as exc:
        cli.entrypoint()

    assert exc.value.code == 7
