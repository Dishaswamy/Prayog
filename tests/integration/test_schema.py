"""Database-enforced guarantees, exercised as the non-superuser application role."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import DBAPIError

from prayog_steward.db.engine import set_rls_user
from prayog_steward.db.models import PRIVATE, STEWARD, metadata


@pytest.fixture
def conn(app_engine: Engine) -> Iterator[Connection]:
    with app_engine.connect() as c:
        tx = c.begin()
        yield c
        tx.rollback()


def _as_user(c: Connection, user_id: str | None) -> None:
    if user_id is None:
        c.execute(text("RESET app.user_id"))
    elif user_id == "":
        c.execute(text("SELECT set_config('app.user_id', '', true)"))
    else:
        set_rls_user(c, user_id)


def _insert_message(c: Connection, user_id: str, attempt_id: str) -> None:
    c.execute(
        text(
            "INSERT INTO steward_private.s3_messages"
            " (message_id, user_id, attempt_id, role, content, created_at)"
            " VALUES (:id, :u, :a, 'user', '{}'::jsonb, now())"
        ),
        {"id": uuid.uuid4(), "u": user_id, "a": attempt_id},
    )


def _count_messages(c: Connection) -> int:
    return int(c.execute(text("SELECT count(*) FROM steward_private.s3_messages")).scalar_one())


def test_models_match_the_migrated_schema(admin_engine: Engine) -> None:
    """models.py is what queries use; the migration is the DDL of record."""
    insp = inspect(admin_engine)
    for table in metadata.sorted_tables:
        assert table.schema in (STEWARD, PRIVATE)
        live = {c["name"] for c in insp.get_columns(table.name, schema=table.schema)}
        assert live == {c.name for c in table.columns}, table.fullname


def test_app_role_cannot_bypass_rls(app_engine: Engine) -> None:
    with app_engine.connect() as c:
        row = c.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert tuple(row) == (False, False)


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
    conn.execute(
        text(
            "INSERT INTO steward.diagnoses (diagnosis_id, run_id, project_id, owner_id, outcome,"
            " claims, matching_earlier_failures, stripped_count, agent_id, agent_version,"
            " created_at) VALUES (:d, 'r1', 'p1', 'u1', 'ok', '[]', '{}', 0, 'stub', '0', now())"
        ),
        {"d": uuid.uuid4()},
    )
    with pytest.raises(DBAPIError):
        conn.execute(text("UPDATE steward.diagnoses SET outcome = 'refused'"))


def _open_warning(conn: Connection, unit: str) -> uuid.UUID:
    wid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO steward.hardware_warnings (warning_id, unit_signature, node_id, opened_at)"
            " VALUES (:w, :u, 'n1', now())"
        ),
        {"w": wid, "u": unit},
    )
    return wid


def test_fr_gpu_164_only_one_open_warning_per_unit(conn: Connection) -> None:
    _open_warning(conn, "n1/gpu0")
    with pytest.raises(DBAPIError):
        _open_warning(conn, "n1/gpu0")


def test_fr_gpu_163_closing_a_warning_needs_a_reason(conn: Connection) -> None:
    wid = _open_warning(conn, "n1/gpu1")
    with pytest.raises(DBAPIError):
        conn.execute(
            text(
                "UPDATE steward.hardware_warnings SET state = 'DISMISSED', closed_at = now(),"
                " closed_by = 'admin' WHERE warning_id = :w"
            ),
            {"w": wid},
        )


def test_closed_warning_cannot_be_reopened(conn: Connection) -> None:
    wid = _open_warning(conn, "n1/gpu2")
    close = text(
        "UPDATE steward.hardware_warnings SET state = :s, closed_at = now(), closed_by = 'admin',"
        " close_reason = 'checked' WHERE warning_id = :w"
    )
    conn.execute(close, {"s": "DISMISSED", "w": wid})
    with pytest.raises(DBAPIError):
        conn.execute(
            text(
                "UPDATE steward.hardware_warnings SET state = 'OPEN', closed_at = NULL"
                " WHERE warning_id = :w"
            ),
            {"w": wid},
        )
