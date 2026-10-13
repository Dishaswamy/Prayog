"""
Database fixtures for the tests marked db.

They run against a separate database, prayog_test, on the development
PostgreSQL (docker compose up -d), created on first use with the .env
credentials and rebuilt from the models at the start of each test session.
Every test starts from empty tables. Without the container the db tests are
skipped and the pure policy tests still run.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

TEST_DB = "prayog_test"


@pytest.fixture(scope="session")
def db_engine():
    try:
        from database import DATABASE_URL
    except RuntimeError as exc:                  # no .env
        pytest.skip(f"database settings missing: {exc}")
    import scheduler.models  # noqa: F401  registers the GPU tables on Base
    from models import Base

    admin = create_engine(DATABASE_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB}):
                conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except OperationalError:
        pytest.skip("PostgreSQL is not reachable: run docker compose up -d")
    finally:
        admin.dispose()

    # Enough connections for the concurrency tests, one per thread.
    engine = create_engine(DATABASE_URL.set(database=TEST_DB), pool_size=20, max_overflow=0)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_sessions(db_engine):
    """A session factory over empty tables. Each concurrent actor opens its own session."""
    from models import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=db_engine, autoflush=False)


@pytest.fixture
def db(db_sessions):
    with db_sessions() as session:
        yield session
