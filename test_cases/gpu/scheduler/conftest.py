"""
Database fixtures for the tests marked db.

They run against their own database, prayog_scheduler_test, on the configured
PostgreSQL (docker compose up -d), created on first use with the same
credentials and rebuilt from the models at the start of each test session.
It is never the database the other backend tests and CI migrate.
Every test starts from empty tables. Without the container the db tests are
skipped and the pure policy tests still run.
"""

import pytest
from hypothesis import HealthCheck, settings
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

TEST_DB = "prayog_scheduler_test"

# Timing checks fail on a loaded machine without saying anything about the rules under test.
settings.register_profile("scheduler", deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile("scheduler")


def pytest_configure(config):
    config.addinivalue_line("markers", "db: needs the PostgreSQL container (docker compose up -d)")


@pytest.fixture(scope="session")
def db_engine():
    try:
        from database import DATABASE_URL
    except RuntimeError as exc:                  # no .env
        pytest.skip(f"database settings missing: {exc}")
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
    import mlflow_models  # noqa: F401  (its tables reference ours, so they are built and dropped together)

    with engine.begin() as conn:                 # also clears tables an earlier run's models defined
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
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


@pytest.fixture
def seed(db):
    from test_cases.gpu.scheduler.db_factories import Seed

    return Seed(db)


@pytest.fixture
def labs(seed):
    ids = {name: seed.lab(name) for name in ("NLP", "ROBOTICS", "VISION")} | {
        "CENTRAL": seed.lab("CENTRAL", central=True),
        "COURSE": seed.lab("CS455 course group"),
    }
    seed.commit()                 # committed, so a refused action that rolls back keeps them
    return ids
