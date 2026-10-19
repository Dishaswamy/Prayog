"""Entry point and the before/after-login command split (spec 4.3, FR-AUTH-20..29, 47..50).

Logged out, the CLI offers exactly two things: `login` and `help`. Everything else (including
--version, logout, and the names of the other commands) is treated alike: the answer is "you
are not logged in", so a logged-out person learns nothing about what exists. After login the
list depends on the role. This is a courtesy and a tidy surface; the server still authorizes
every request (NFR-AUTH-03).
"""
import argparse
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import httpx

from prayog import __version__
from prayog.commands import access, auth, help as help_cmd, project, review
from prayog.context import Context
from prayog.credentials import Credentials, load
from prayog.errors import CliError, INSUFFICIENT_PERMISSION, NOT_AUTHENTICATED, USAGE


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    setup: Callable
    protected: bool = True              # needs a saved login
    roles: Optional[tuple] = None       # only these roles; None = any logged-in user


COMMANDS = (
    Command("login", "sign in with your institute email", auth.setup_login, protected=False),
    Command("help", "show help for prayog or for one command", help_cmd.setup_help, protected=False),
    Command("logout", "end your session and remove the saved login", auth.setup_logout),
    Command("whoami", "show who you are logged in as", auth.setup_whoami),
    Command("project", "create and manage your projects", project.setup_project,
            roles=("STUDENT", "RESEARCHER", "PROFESSOR")),
    Command("access", "check the status of your access request", access.setup_access,
            roles=("STUDENT", "RESEARCHER")),
    Command("requests", "review, approve, reject or revoke access requests", review.setup_requests,
            roles=("PROFESSOR", "ADMIN")),
)

LOGIN_HINT = "You are not logged in. Run `prayog login`."


def available(creds: Optional[Credentials]) -> tuple:
    """The commands this person may use right now."""
    return tuple(c for c in COMMANDS
                 if not c.protected or (creds is not None and (c.roles is None or creds.role in c.roles)))


def denial(name: str, creds: Optional[Credentials]) -> CliError:
    """Why `name` is not available. Logged out, every unavailable name gets the same answer."""
    if creds is None:
        return CliError(LOGIN_HINT, NOT_AUTHENTICATED)
    if any(c.name == name for c in COMMANDS):
        return CliError(f"`prayog {name}` is not available for your role ({creds.role}). "
                        "Run `prayog help` to see what you can do.", INSUFFICIENT_PERMISSION)
    return CliError(f"Unknown command '{name}'. Run `prayog help` to see what you can do.", USAGE)


def build_parser(creds: Optional[Credentials]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prayog",
        description="Prayog command line." + ("" if creds else " Run `prayog login` to get started."),
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="show this help message and exit")
    if creds is not None:
        parser.add_argument("--version", action="version", version=f"prayog {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for cmd in available(creds):
        cmd.setup(sub.add_parser(cmd.name, help=cmd.summary, description=cmd.summary))
    return parser


def _gate(argv: Sequence[str], creds: Optional[Credentials]) -> None:
    """Refuse, before argparse sees anything, a first word that is not available to this person.
    argparse would otherwise print usage for hidden commands or list their names in its errors."""
    if not argv:
        return
    first = argv[0]
    if first.startswith("-"):
        if first == "--version" and creds is None:
            raise denial(first, creds)
        return
    if first not in {c.name for c in available(creds)}:
        raise denial(first, creds)


def main(argv: Optional[Sequence[str]] = None, *, transport: Optional[httpx.BaseTransport] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    creds = load()
    try:
        _gate(argv, creds)
        parser = build_parser(creds)
        args = parser.parse_args(argv)
        if args.command is None:
            parser.print_help()
            return 0
        command = next(c for c in COMMANDS if c.name == args.command)
        if command.protected and creds is None:       # defence in depth: _gate already refused this
            raise denial(command.name, creds)
        args.func(Context(creds=creds, transport=transport, build_parser=build_parser, available=available,
                          denial=denial), args)
    except CliError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 2 if exc.code == USAGE else 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


def entrypoint() -> None:
    sys.exit(main())
