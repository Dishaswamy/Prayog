"""Engine and transaction helpers.

``private_tx`` is the only way server code should touch ``steward_private``: it opens a
transaction and sets ``app.user_id`` for it, so row-level security filters every
statement to that user's rows (NFR-SEC-002, ADR-19). The setting is transaction-local
and disappears at commit or rollback.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine, text


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True)


def set_rls_user(conn: Connection, user_id: str) -> None:
    if not user_id:
        raise ValueError("an authenticated user id is required for private data")
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": user_id})


@contextmanager
def private_tx(engine: Engine, user_id: str) -> Iterator[Connection]:
    with engine.begin() as conn:
        set_rls_user(conn, user_id)
        yield conn
