"""The local login state: one JSON file holding the access token, never the password.

Lives in ~/.prayog (override with PRAYOG_CONFIG_DIR), directory 0700, file 0600.
`prayog logout` deletes the file; the installed package is untouched.
"""
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Credentials:
    api_url: str
    token: str
    email: str
    user_id: str
    role: str


def config_dir() -> Path:
    override = os.environ.get("PRAYOG_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".prayog"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def load() -> Optional[Credentials]:
    """The saved login, or None. An unreadable or incomplete file counts as logged out."""
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        return Credentials(**{k: data[k] for k in Credentials.__dataclass_fields__})
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(creds: Credentials) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    # Write to a private temp file, then rename: the token is never world-readable, even briefly.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".credentials-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(creds), fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, credentials_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear() -> bool:
    """Remove the saved login. True if there was one."""
    try:
        credentials_path().unlink()
        return True
    except FileNotFoundError:
        return False
