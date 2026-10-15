"""
Backend tests run against a real PostgreSQL. PRAYOG_TEST_ADMIN_URL is a superuser URL
to a server (default: the throwaway cluster from `python steward/tools/dev_postgres.py
start`). A fresh database is created per session, migrated with the platform's own
Alembic history, then prepared for the Steward exactly as in production
(scripts.setup_steward). Tables are emptied between tests.
"""

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

ADMIN_URL = make_url(os.environ.get(
    "PRAYOG_TEST_ADMIN_URL", "postgresql+psycopg://postgres@127.0.0.1:55432/postgres"
))
TEST_DB = "prayog_platform_test"
PASSWORD = "test-password-1"
STEWARD_ROLE, STEWARD_PASSWORD = "steward_app_ptest", "steward-app-ptest"

# The platform reads its configuration at import time: set it before any import.
os.environ.update({
    "POSTGRES_USER": ADMIN_URL.username or "postgres",
    "POSTGRES_PASSWORD": ADMIN_URL.password or "unused-with-trust-auth",
    "POSTGRES_HOST": ADMIN_URL.host or "127.0.0.1",
    "POSTGRES_PORT": str(ADMIN_URL.port or 5432),
    "POSTGRES_DB": TEST_DB,
    "JWT_SECRET_KEY": "test-secret-key-not-for-production",
    "STEWARD_DB_USER": STEWARD_ROLE,
    "STEWARD_DB_PASSWORD": STEWARD_PASSWORD,
    "STEWARD_INPROCESS_REASONING": "heuristic",
    "STEWARD_KNOWLEDGE_EMBEDDER": "hashing",
    "STEWARD_SEARCH_PROVIDER": "none",
})


def _create_database() -> None:
    server = create_engine(ADMIN_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with server.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {TEST_DB}"))
    server.dispose()


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    _create_database()
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")

    from scripts.setup_steward import setup

    setup(STEWARD_ROLE, STEWARD_PASSWORD)
    yield


@pytest.fixture(autouse=True)
def _clean(database: None) -> Iterator[None]:
    yield
    from database import engine

    with engine.begin() as c:
        tables = c.execute(text(
            "SELECT schemaname || '.' || tablename FROM pg_tables "
            "WHERE schemaname IN ('public', 'steward', 'steward_private') "
            "AND tablename <> 'alembic_version'"
        )).scalars().all()
        c.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@dataclass
class Agent:
    """A node agent's identity in a test: its credential and report sequence (FR-GPU-128)."""
    credential: str
    seq: int = 0

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Node-Credential": self.credential}


@pytest.fixture
def world(database: None) -> dict:
    """The seeded development world (admin, prof, alice, bob) with nodes n1 and n2
    online: each enrolled with its one-time token and heartbeating, as its agent would."""
    from database import SessionLocal
    from logic.scheduler.nodes import enrol, heartbeat
    from scripts.seed_dev import UNITS, seed

    agents = {}
    with SessionLocal() as db:
        for name, token in seed(PASSWORD).items():
            node, credential = enrol(db, token)
            heartbeat(db, node.id, UNITS, [])
            agents[name] = Agent(credential)
    return {"password": PASSWORD, "agents": agents}


@pytest.fixture
def client(world: dict):
    from fastapi.testclient import TestClient

    import main

    assert main.STEWARD_INSTALLED, "the Steward must be mounted in the test app"
    with TestClient(main.app) as c:
        yield c
