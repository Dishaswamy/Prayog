"""A throwaway local PostgreSQL cluster for development and tests, no Docker needed.

Uses PostgreSQL server binaries already installed on the machine (PATH, or the usual
install locations on Windows and Linux). The cluster lives in ``.pgdata/`` (gitignored),
listens on 127.0.0.1:55432 and trusts local connections, so never expose it.

    python tools/dev_postgres.py start    # init on first use, then start
    python tools/dev_postgres.py url      # print the superuser URL for STEWARD_TEST_DATABASE_URL
    python tools/dev_postgres.py stop
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".pgdata"
PORT = int(os.environ.get("STEWARD_DEV_PG_PORT", "55432"))
DB = "prayog"
URL = f"postgresql+psycopg://postgres@127.0.0.1:{PORT}/{DB}"


def _bin(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    found = shutil.which(exe)
    if found:
        return found
    patterns = [
        r"C:\Program Files\PostgreSQL\*\bin",
        "/usr/lib/postgresql/*/bin",
        "/opt/homebrew/opt/postgresql@*/bin",
        "/usr/local/opt/postgresql@*/bin",
    ]
    for pattern in patterns:
        for directory in sorted(glob.glob(pattern), reverse=True):
            candidate = Path(directory) / exe
            if candidate.exists():
                return str(candidate)
    sys.exit(f"{name} not found; install PostgreSQL server binaries (15 or newer)")


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def init() -> None:
    if (DATA / "PG_VERSION").exists():
        return
    DATA.mkdir(exist_ok=True)
    _run(_bin("initdb"), "-D", str(DATA), "-U", "postgres", "--auth=trust", "-E", "UTF8",
         "--locale=C")
    with open(DATA / "postgresql.conf", "a", encoding="utf-8") as conf:
        conf.write(f"\nport = {PORT}\nlisten_addresses = '127.0.0.1'\n")


def running() -> bool:
    return _run(_bin("pg_ctl"), "-D", str(DATA), "status", check=False).returncode == 0


def start() -> None:
    init()
    if not running():
        # The server inherits stdio, so never capture it: pg_ctl would never return.
        subprocess.run(
            [_bin("pg_ctl"), "-D", str(DATA), "-l", str(DATA / "server.log"), "-w", "start"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    created = _run(
        _bin("psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname = '{DB}'",
    )
    if created.stdout.strip() != "1":
        _run(_bin("createdb"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", DB)
    print(URL)


def stop() -> None:
    if running():
        _run(_bin("pg_ctl"), "-D", str(DATA), "-w", "stop")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if command == "start":
        start()
    elif command == "stop":
        stop()
    elif command == "url":
        print(URL)
    elif command == "status":
        print("running" if DATA.exists() and running() else "stopped")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
