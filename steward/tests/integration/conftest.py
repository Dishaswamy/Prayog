"""PostgreSQL fixtures. Tests here need STEWARD_TEST_DATABASE_URL: a superuser URL to a
scratch database (``python tools/dev_postgres.py start`` prints one; CI uses a service).

The schema is rebuilt once per session. Application code runs as ``steward_app_test``, a
role without superuser or BYPASSRLS, exactly as in production, so RLS is really exercised.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text

ROOT = Path(__file__).resolve().parents[2]

# Shared API fixture: the full Steward over HTTP with fakes (used by several test modules).
from test_api import world  # noqa: E402, F401

APP_ROLE = "steward_app_test"
APP_PASSWORD = "steward_app_test"  # test-only role on a scratch database


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.postgres)


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[Engine]:
    url = os.environ["STEWARD_TEST_DATABASE_URL"]
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS steward_private CASCADE"))
        c.execute(text("DROP SCHEMA IF EXISTS steward CASCADE"))
        exists = c.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}
        ).scalar()
        if not exists:
            c.execute(
                text(
                    f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'"
                    " NOSUPERUSER NOBYPASSRLS"
                )
            )
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/prayog_steward/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    os.environ["STEWARD_APP_ROLE"] = APP_ROLE
    try:
        command.upgrade(cfg, "head")
    finally:
        os.environ.pop("STEWARD_APP_ROLE", None)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_url(admin_engine: Engine) -> str:
    url = make_url(os.environ["STEWARD_TEST_DATABASE_URL"])
    return url.set(username=APP_ROLE, password=APP_PASSWORD).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def app_engine(app_url: str) -> Iterator[Engine]:
    engine = create_engine(app_url)
    yield engine
    engine.dispose()


TABLES = [
    "steward.knowledge_chunks",
    "steward.knowledge_documents",
    "steward.warning_evidence",
    "steward.hardware_warnings",
    "steward.proposals",
    "steward.hardware_findings",
    "steward.diagnoses",
    "steward.fault_evidence",
    "steward.failure_events",
    "steward.steward_tasks",
    "steward_private.steward_links",
    "steward_private.s3_sessions",
    "steward_private.rag_chunks",
    "steward_private.s3_messages",
    "steward_private.response_cache",
]


@pytest.fixture(autouse=True)
def _clean(admin_engine: Engine) -> Iterator[None]:
    yield
    with admin_engine.begin() as c:
        c.execute(text(f"TRUNCATE {', '.join(TABLES)}"))
