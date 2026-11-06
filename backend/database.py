import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set in .env")
    return value


DATABASE_URL = URL.create(
    drivername="postgresql+psycopg2",
    username=_require("POSTGRES_USER"),
    password=_require("POSTGRES_PASSWORD"),   # special characters are escaped safely
    host=_require("POSTGRES_HOST"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    database=_require("POSTGRES_DB"),
)

# FastAPI runs every sync endpoint on AnyIO's thread pool, 40 threads by default, and each
# request holds one connection. SQLAlchemy's default pool (5 + 10 overflow) made threads 16-40
# wait for a connection and fail after 30 s under load (Deliverable 3 load test). Size the pool
# to the thread pool; with several uvicorn workers, keep workers x (size + overflow) below
# PostgreSQL's max_connections or put PgBouncer in front.
POOL_SIZE = int(os.getenv("PRAYOG_DB_POOL_SIZE", "20"))
MAX_OVERFLOW = int(os.getenv("PRAYOG_DB_MAX_OVERFLOW", "20"))
POOL_TIMEOUT = float(os.getenv("PRAYOG_DB_POOL_TIMEOUT_SECONDS", "10"))   # then 503 (main.py), not a 30 s hang

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # checks a connection is alive before using it (avoids errors after Postgres restarts)
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()