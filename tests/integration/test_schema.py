"""Database-enforced guarantees, against a real PostgreSQL + pgvector.

Needs STEWARD_TEST_DATABASE_URL pointing at a superuser connection to a scratch database.
The schema is rebuilt for the module, and RLS is exercised through a non-superuser role,
because superusers bypass row-level security.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
APP_ROLE = "steward_app_test"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    url = os.environ["STEWARD_TEST_DATABASE_URL"]
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS steward_private CASCADE"))
        c.execute(text("DROP SCHEMA IF EXISTS steward CASCADE"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/prayog_steward/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    with eng.begin() as c:
        c.execute(text(f"DROP ROLE IF EXISTS {APP_ROLE}"))
        c.execute(text(f"CREATE ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS"))
        c.execute(text(f"GRANT USAGE ON SCHEMA steward, steward_private TO {APP_ROLE}"))
        c.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                f"steward, steward_private TO {APP_ROLE}"
            )
        )
    yield eng
    with eng.begin() as c:
        c.execute(text(f"DROP OWNED BY {APP_ROLE}"))
        c.execute(text(f"DROP ROLE {APP_ROLE}"))
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection acting as the application role, rolled back after each test."""
    with engine.connect() as c:
        tx = c.begin()
        c.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        yield c
        tx.rollback()


def _as_user(c: Connection, user_id: str | None) -> None:
    if user_id is None:
        c.execute(text("RESET app.user_id"))
    else:
        c.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": user_id})


def _insert_message(c: Connection, user_id: str, attempt_id: str) -> None:
    c.execute(
        text(
            "INSERT INTO steward_private.s3_messages"
            " (message_id, user_id, attempt_id, role, content)"
            " VALUES (:id, :u, :a, 'user', '{}'::jsonb)"
        ),
        {"id": uuid.uuid4(), "u": user_id, "a": attempt_id},
    )


def _count_messages(c: Connection) -> int:
    return int(c.execute(text("SELECT count(*) FROM steward_private.s3_messages")).scalar_one())


def test_a03_nfr_sec_002_each_user_sees_only_their_rows(conn: Connection) -> None:
    _as_user(conn, "alice")
    _insert_message(conn, "alice", "a1")
    _as_user(conn, "bob")
    _insert_message(conn, "bob", "b1")
    assert _count_messages(conn) == 1
    _as_user(conn, "alice")
    assert _count_messages(conn) == 1


def test_a03_missing_identity_default_denies(conn: Connection) -> None:
    _as_user(conn, "alice")
    _insert_message(conn, "alice", "a1")
    _as_user(conn, None)
    assert _count_messages(conn) == 0
    _as_user(conn, "")
    assert _count_messages(conn) == 0


def test_cannot_write_rows_for_another_user(conn: Connection) -> None:
    _as_user(conn, "bob")
    with pytest.raises(DBAPIError):
        _insert_message(conn, "alice", "a1")


def test_nfr_sec_001_owner_and_attempt_are_immutable(conn: Connection) -> None:
    _as_user(conn, "alice")
    _insert_message(conn, "alice", "a1")
    with pytest.raises(DBAPIError):
        conn.execute(text("UPDATE steward_private.s3_messages SET attempt_id = 'a2'"))


def test_nfr_gpu_057_diagnoses_are_append_only(conn: Connection) -> None:
    did = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO steward.diagnoses (diagnosis_id, run_id, owner_id, outcome, claims,"
            " matching_earlier_failures, stripped_count, agent_id, agent_version)"
            " VALUES (:d, 'r1', 'u1', 'ok', '[]', '{}', 0, 'stub', '0')"
        ),
        {"d": did},
    )
    with pytest.raises(DBAPIError):
        conn.execute(text("UPDATE steward.diagnoses SET outcome = 'refused'"))


def test_fr_gpu_164_only_one_open_warning_per_unit(conn: Connection) -> None:
    insert = text(
        "INSERT INTO steward.hardware_warnings (warning_id, unit_signature, node_id)"
        " VALUES (:w, 'n1/gpu0', 'n1')"
    )
    conn.execute(insert, {"w": uuid.uuid4()})
    with pytest.raises(DBAPIError):
        conn.execute(insert, {"w": uuid.uuid4()})


def test_fr_gpu_163_closing_a_warning_needs_a_reason(conn: Connection) -> None:
    wid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO steward.hardware_warnings (warning_id, unit_signature, node_id)"
            " VALUES (:w, 'n1/gpu1', 'n1')"
        ),
        {"w": wid},
    )
    with pytest.raises(DBAPIError):
        conn.execute(
            text(
                "UPDATE steward.hardware_warnings SET state = 'DISMISSED', closed_at = now(),"
                " closed_by = 'admin' WHERE warning_id = :w"
            ),
            {"w": wid},
        )
