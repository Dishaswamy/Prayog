"""Alembic environment. Our version table lives in schema ``steward`` so these migrations
never collide with the platform's (ADR-S07)."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from prayog_steward.config import StewardSettings
from prayog_steward.db.models import STEWARD, metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

URL = config.get_main_option("sqlalchemy.url") or StewardSettings().database_url


def run_offline() -> None:
    context.configure(
        url=URL,
        literal_binds=True,
        target_metadata=metadata,
        version_table_schema=STEWARD,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.execute(f"CREATE SCHEMA IF NOT EXISTS {STEWARD}")
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(URL)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            version_table_schema=STEWARD,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.execute(f"CREATE SCHEMA IF NOT EXISTS {STEWARD}")
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
