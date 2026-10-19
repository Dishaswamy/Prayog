# Prayog CLI

Command-line client for the Prayog server. Implements the CLI parts of the
Authentication and Authorization specification (installation, login, protected
operations, logout).

```bash
pip install prayog        # installs the CLI only: no login, no access (FR-AUTH-20/21)
prayog login              # authenticates (FR-AUTH-23/24)
prayog logout             # ends the session on the server and removes the local login; the CLI stays installed (FR-AUTH-47..50)
```

From this repo: `pip install -e ./cli`.

## Web portal vs CLI

Account registration and **access requests are done in the web portal**, not the CLI (spec UC-A01, UC-A02).
The CLI is for signing in and for the commands you use once you have access. Students and researchers can
check how their request is going with `prayog access status`.

## What is available when

| State | Commands |
|---|---|
| Not logged in | **only** `prayog login` and `prayog help` (`help`, `-h` and `--help` are the same) |
| Logged in (any role) | `help`, `--version`, `whoami`, `logout` |
| Student / Researcher / Professor | `project create NAME [-d DESCRIPTION]` |
| Student / Researcher | `access status`; `access request` only explains that requests are made in the web portal |
| Professor / Admin | `requests list`, `requests show ID`, `requests approve ID`, `requests reject ID` |
| Admin | `requests revoke ID` |

Before login, anything other than `login` and `help` (including `logout`, `--version`, a mistyped
command, or `--help` on a hidden command) gets the same answer and nothing else:
`Error: You are not logged in. Run `prayog login`.` (FR-AUTH-28/29). The CLI does not contact the server,
and does not reveal which other commands exist. After login, `prayog help` lists what your role can use;
a command meant for another role says so. This is a tidy surface, not security: the server authorizes every
request again (FR-AUTH-30, NFR-AUTH-03).

`pip install prayog` only installs the program: it creates no login and grants no access (FR-AUTH-20/21).

## Login

```bash
prayog login                                   # prompts for email, role, password
prayog login -e dishac22@iitk.ac.in -r student # prompts for the password only
echo "$PW" | prayog login -e dishac22@iitk.ac.in -r student --password-stdin   # scripts
```

- The server needs the role to match the account (`/auth/login`).
- The password is never stored (FR-AUTH-27). Only the access token is saved, in
  `~/.prayog/credentials.json` (directory `0700`, file `0600`; override the directory with
  `PRAYOG_CONFIG_DIR`).
- Server: `--api-url`, or `PRAYOG_API_URL`, default `http://localhost:8000`. The URL is saved with the
  token, and later commands only ever send the token to that URL.
- Plain `http` is refused except to this machine (NFR-AUTH-02); `--allow-insecure-http` overrides.
- Login takes your institute **email** (not the user ID), the same as the web portal.
- **Only users with current access can log in** (`POST /auth/cli/login`). Someone whose request is pending,
  rejected, expired or revoked is told why and gets no login, so nothing else in the CLI is reachable. The web
  portal still lets them log in to request access. The check happens at login: a login already made keeps
  working until it expires (60 minutes) or is logged out, even if access is revoked meanwhile.
- After login, students and researchers see where their access stands. Authentication success does not
  mean access is active (spec 4.2).

## Using it

```bash
prayog whoami                      # who you are logged in as

prayog project create vision -d "image models"   # makes a project on the server, prints its unique ID

prayog access status               # student/researcher: your request and whether access is active

prayog requests list --status pending   # professor/admin: requests to review
prayog requests show 7
prayog requests approve 7          # or: reject 7
prayog requests revoke 7           # admin only
```

## Logout

`prayog logout` calls `POST /auth/logout`, so the token stops working on the server straight away (this login
only; other logins of yours, such as the web portal, are not affected), then deletes `~/.prayog/credentials.json`.
If the server can't be reached, the local token is still removed and the CLI warns that the old token may stay
valid on the server until it expires (60 minutes). Prayog stays installed either way.

Running `prayog login` while already logged in ends the previous terminal session on the server first.

## Projects

`prayog project create NAME` creates a project for you on the server and prints its unique project ID.
Names are unique per user and case is ignored (`Vision` and `vision` clash); a repeat gets
`You already have a project named 'X'`. The project is only a name and description for now; linking a
folder to it comes later. This replaces `create-project` in `backend/mlflow_cli.py`, which has been
removed: the CLI in this folder talks to the server over HTTP only and shares no code with `backend/`.

## Errors

Messages say what happened and what to do (NFR-AUTH-10). Exit code `0` success, `1` failure, `2` bad usage.

| Situation | Message starts with | Code in the spec |
|---|---|---|
| no saved login | You are not logged in. Run `prayog login`. | NOT_AUTHENTICATED |
| wrong email / password / role | Invalid email, password or role | INVALID_CREDENTIAL |
| no approved access at login | the server's reason: still waiting for approval / rejected / period ended / revoked | INSUFFICIENT_PERMISSION, ACCESS_EXPIRED, ACCESS_REVOKED |
| saved token rejected | The server did not accept your saved login | INVALID_CREDENTIAL |
| token older than 60 min, or password was reset | Your login session has expired. Run `prayog login`. | (session) |
| `ACCESS_EXPIRED` from the server | Your approved access period has ended | ACCESS_EXPIRED |
| `ACCESS_REVOKED` from the server | Your access has been revoked | ACCESS_REVOKED |
| 403 otherwise | the server's reason | INSUFFICIENT_PERMISSION |
| server unreachable | Could not reach the Prayog server | fails closed |

## Tests

```bash
pip install -e './cli[test]'
pytest cli
```

The tests use a mock HTTP transport; no server is needed.

## Not yet possible (backend gaps)

- **Access extension** (FR-AUTH-42..46, UC-A07/A08): no backend endpoint yet. When it exists it will be a
  web portal action, like access requests.
- Infrastructure management (FR-INF) and audit review (FR-AUD) have API endpoints for nodes but no CLI commands yet.
