"""login, logout, whoami (UC-A05, UC-A10)."""
import getpass
import os
import sys

from prayog.client import Client, is_secure_enough
from prayog.commands.access import PORTAL_HINT, describe_state
from prayog.context import Context
from prayog.credentials import Credentials, clear, load, save
from prayog.errors import CliError, INVALID_CREDENTIAL, SESSION_EXPIRED, USAGE
from prayog.ui import prompt

ROLES = ["student", "researcher", "professor", "admin"]
DEFAULT_API_URL = "http://localhost:8000"


def setup_login(p):
    p.add_argument("-e", "--email", help="your institute email address")
    p.add_argument("-r", "--role", choices=ROLES, help="the role your account has")
    p.add_argument("--api-url", help=f"Prayog server (default: $PRAYOG_API_URL or {DEFAULT_API_URL})")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin (for scripts) instead of prompting")
    p.add_argument("--allow-insecure-http", action="store_true",
                   help="allow sending your password over plain http to a non-local server")
    p.set_defaults(func=login)


def login(ctx: Context, args) -> None:
    api_url = (args.api_url or os.environ.get("PRAYOG_API_URL") or DEFAULT_API_URL).rstrip("/")
    if not is_secure_enough(api_url) and not args.allow_insecure_http:
        raise CliError(f"Refusing to send your password to {api_url} over plain http. "
                       "Use an https URL.", USAGE)

    email = prompt("Institute email", args.email, "--email").strip()
    if "@" not in email:
        raise CliError("Enter your full institute email address, e.g. name@iitk.ac.in.", USAGE)
    role = prompt(f"Role ({'/'.join(ROLES)})", args.role, "--role").lower()
    if role not in ROLES:
        raise CliError(f"Role must be one of: {', '.join(ROLES)}.", USAGE)
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    elif sys.stdin.isatty():
        password = getpass.getpass("Password: ")
    else:
        raise CliError("No terminal to prompt on. Pass --password-stdin and pipe the password in.", USAGE)
    if not password:
        raise CliError("Password is required.", USAGE)

    anon = Client(api_url, transport=ctx.transport)
    token = anon.post("/auth/cli/login", json={"email": email, "password": password,
                                           "role": role.upper()})["access_token"]
    # Confirm the token works and learn who we are before saving anything.
    authed = Client(api_url, token, transport=ctx.transport)
    me = authed.get("/auth/me")
    if me["role"] != role.upper():
        raise CliError("The server returned a different role than the one you logged in with.", INVALID_CREDENTIAL)

    # Logging in again from this terminal replaces the saved login; end the old session on the server
    # instead of leaving an unreachable token alive. Best effort: never blocks the new login.
    if ctx.creds is not None:
        try:
            Client(ctx.creds.api_url, ctx.creds.token, transport=ctx.transport).post("/auth/logout")
        except CliError:
            pass

    save(Credentials(api_url=api_url, token=token, email=me["email"], user_id=me["user_id"], role=me["role"]))
    print(f"Logged in as {me['email']} ({me['role']}).")

    # Authentication is not authorization (spec 4.2): say where access stands, but never fail login over it.
    if me["role"] in ("STUDENT", "RESEARCHER"):
        try:
            mine = authed.get("/access-requests/me")
            print(f"Access: {describe_state(mine[0]) if mine else f'no access request yet. {PORTAL_HINT}'}")
        except CliError:
            pass


def setup_logout(p):
    p.set_defaults(func=logout)


def logout(ctx: Context, args) -> None:
    """End the session on the server, then remove the local token (FR-AUTH-48).

    The local token is removed whatever the server says: logging out must always work.
    """
    warning = None
    try:
        ctx.client().post("/auth/logout")
    except CliError as exc:
        # 401 means the server already considers this token dead (expired, logged out elsewhere): nothing to end.
        if exc.code not in (SESSION_EXPIRED, INVALID_CREDENTIAL):
            warning = exc.message
    clear()
    if warning:
        print(f"Warning: could not end the session on the server ({warning}). "
              "The token was removed from this computer but may stay valid there until it expires.",
              file=sys.stderr)
    print("Logged out. Prayog stays installed; run `prayog login` to sign in again.")


def setup_whoami(p):
    p.set_defaults(func=whoami)


def whoami(ctx: Context, args) -> None:
    me = ctx.client().get("/auth/me")
    print(f"{me['email']}  (user id {me['user_id']}, role {me['role']})")
    print(f"Server: {ctx.creds.api_url}")
