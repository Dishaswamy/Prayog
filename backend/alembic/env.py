import os
import sys

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from alembic import context

# Make the backend folder importable (models.py, database.py)
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BACKEND_DIR)

# Load .env BEFORE importing database.py (it reads env vars at import time)
ENV_FILE = os.path.abspath(os.path.join(BACKEND_DIR, "../.env"))
load_dotenv(ENV_FILE)

from database import DATABASE_URL  # noqa: E402  same URL the app uses
from models import Base             # noqa: E402  imports all models

config = context.config
config.set_main_option(
    "sqlalchemy.url",
    DATABASE_URL.render_as_string(hide_password=False).replace("%", "%%"),
)

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_server_default=True,   # detect changed defaults (e.g. UTC timestamps)
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()