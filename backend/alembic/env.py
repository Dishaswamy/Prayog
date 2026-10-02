import os
import sys

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import URL
from alembic import context

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from models import Base

ENV_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../.env")
)

load_dotenv(ENV_FILE)

print("ENV FILE:", ENV_FILE)
print("POSTGRES_HOST:", os.getenv("POSTGRES_HOST"))
print("POSTGRES_PORT:", os.getenv("POSTGRES_PORT"))

config = context.config

DATABASE_URL = URL.create(
    drivername="postgresql",
    username=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    host=os.getenv("POSTGRES_HOST"),
    port=int(os.getenv("POSTGRES_PORT")),
    database=os.getenv("POSTGRES_DB"),
)

url_str = DATABASE_URL.render_as_string(hide_password=False)
config.set_main_option("sqlalchemy.url", url_str.replace("%", "%%"))

target_metadata = Base.metadata
print("TABLES:", Base.metadata.tables.keys())

def run_migrations_offline():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()