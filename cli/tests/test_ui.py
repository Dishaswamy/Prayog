import pytest

from prayog import ui
from prayog.errors import CliError, USAGE


def test_prompt_returns_provided_value():
    assert ui.prompt("Email", "test@example.com", "--email") == "test@example.com"


def test_prompt_rejects_missing_value_without_terminal(monkeypatch):
    monkeypatch.setattr(ui.sys, "stdin", type("FakeStdin", (), {
        "isatty": lambda self: False
    })())

    with pytest.raises(CliError, match="Missing --email") as exc:
        ui.prompt("Email", None, "--email")

    assert exc.value.code == USAGE


def test_prompt_rejects_empty_terminal_input(monkeypatch):
    monkeypatch.setattr(ui.sys, "stdin", type("FakeStdin", (), {
        "isatty": lambda self: True
    })())
    monkeypatch.setattr("builtins.input", lambda _: "   ")

    with pytest.raises(CliError, match="Email is required") as exc:
        ui.prompt("Email", None, "--email")

    assert exc.value.code == USAGE


def test_prompt_returns_stripped_terminal_input(monkeypatch):
    monkeypatch.setattr(ui.sys, "stdin", type("FakeStdin", (), {
        "isatty": lambda self: True
    })())
    monkeypatch.setattr("builtins.input", lambda _: "  test@example.com  ")

    assert ui.prompt("Email", None, "--email") == "test@example.com"