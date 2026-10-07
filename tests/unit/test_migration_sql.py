"""Static checks on the migration's generated SQL (offline mode, no database needed).
The behaviour itself is proven in tests/integration against a real PostgreSQL."""

import re
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_TABLES = ["steward_links", "rag_chunks", "s3_messages", "response_cache"]


@pytest.fixture(scope="module")
def sql() -> str:
    buffer = StringIO()
    cfg = Config(str(ROOT / "alembic.ini"), stdout=buffer)
    cfg.set_main_option("script_location", str(ROOT / "src/prayog_steward/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://x:y@localhost/prayog")
    cfg.output_buffer = buffer
    command.upgrade(cfg, "head", sql=True)
    return buffer.getvalue()


def test_version_table_lives_in_steward_schema(sql: str) -> None:
    assert "CREATE TABLE steward.alembic_version" in sql


@pytest.mark.parametrize("table", PRIVATE_TABLES)
def test_nfr_sec_002_every_private_table_forces_rls_default_deny(sql: str, table: str) -> None:
    assert f"ALTER TABLE steward_private.{table} FORCE ROW LEVEL SECURITY" in sql
    policy = re.search(
        rf"CREATE POLICY {table}_owner_only ON steward_private.{table}(.*?);", sql, re.S
    )
    assert policy is not None
    assert "current_setting('app.user_id', true)" in policy.group(1)
    assert f"{table}_keep_owner" in sql  # NFR-SEC-001: owner is immutable


@pytest.mark.parametrize(
    "trigger",
    [
        "diagnoses_append_only",  # NFR-GPU-057
        "hardware_findings_append_only",
        "failure_events_append_only",
        "fault_evidence_consume_only",  # FR-GPU-163
        "proposals_decide_once",  # FR-GPU-167
    ],
)
def test_append_only_guarantees_are_installed(sql: str, trigger: str) -> None:
    assert f"CREATE TRIGGER {trigger}" in sql


def test_fr_gpu_164_one_open_warning_per_unit_signature(sql: str) -> None:
    pattern = r"CREATE UNIQUE INDEX hardware_warnings_one_open.*?WHERE state = 'OPEN'"
    assert re.search(pattern, sql, re.S)
