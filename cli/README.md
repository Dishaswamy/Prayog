# Prayog CLI

Command-line client for the Prayog server. Implements the CLI parts of the
Authentication and Authorization specification (installation, login, protected
operations, logout).

```bash
pip install prayog        # installs the CLI only: no login, no access (FR-AUTH-20/21)
prayog login              # authenticates (FR-AUTH-23/24)
prayog logout             # removes the local login; the CLI stays installed (FR-AUTH-47..50)
```

From this repo: `pip install -e ./cli`.

## What is available when

| State | Commands |
|---|---|
| Not logged in | `prayog --help`, `prayog --version`, `prayog login` |
| Logged in (any role) | `whoami`, `professors`, `logout` |
| Student / Researcher | `access status`, `access request` |
| Professor / Admin | `requests list`, `requests show ID`, `requests approve ID`, `requests reject ID` |
| Admin | `requests revoke ID` |

Before login the help lists only `login`. Every other command is hidden from help and, if run,
prints `You are not logged in. Run `prayog login`.` without contacting the server (FR-AUTH-28/29).
Hiding is a courtesy only: the server authorizes every request again (FR-AUTH-30, NFR-AUTH-03).

## Login

```bash
prayog login                                   # prompts for email, role, password
prayog login -e dishac22@iitk.ac.in -r student   # prompts for the password only
echo "$PW" | prayog login -e dishac22@iitk.ac.in -r student --password-stdin   # scripts
```

- The server needs the role to match the account (`/auth/login`).
- The password is never stored (FR-AUTH-27). Only the access token is saved, in
  `~/.prayog/credentials.json` (directory `0700`, file `0600`; override the directory with
  `PRAYOG_CONFIG_DIR`).
- Server: `--api-url`, or `PRAYOG_API_URL`, default `http://localhost:8000`. The URL is saved with the
  token, and later commands only ever send the token to that URL.
- Plain `http` is refused except to this machine (NFR-AUTH-02); `--allow-insecure-http` overrides.
- After login, students and researchers see where their access stands. Authentication success does not
  mean access is active (spec 4.2).

## Errors
Messages say what happened and what to do (NFR-AUTH-10). Exit code `0` success, `1` failure, `2` bad usage.

| Situation | Message starts with | Code in the spec |
|---|---|---|
| no saved login | You are not logged in. Run `prayog login`. | NOT_AUTHENTICATED |
| wrong email / password / role | Invalid email, password or role | INVALID_CREDENTIAL |
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

- **Access extension** (FR-AUTH-42..46, UC-A07/A08): no backend endpoint yet, so no CLI command.
- **Server-side logout**: tokens are stateless JWTs and there is no logout endpoint. `prayog logout` deletes the
  local token; a copy of it would stay valid until it expires (60 min).
- **ACCESS_EXPIRED / ACCESS_REVOKED responses**: the CLI maps these `code` values, but the backend does not
  send them yet. Today an expired or revoked student authenticates fine and `prayog access status` shows the
  state; protected job endpoints answer `AUTHORIZATION_INACTIVE`.
- Infrastructure management (FR-INF) and audit review (FR-AUD) have API endpoints for nodes but no CLI commands yet.
