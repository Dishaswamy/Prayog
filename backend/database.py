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

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # checks a connection is alive before using it (avoids errors after Postgres restarts)
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()