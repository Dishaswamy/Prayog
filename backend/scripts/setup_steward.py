"""
Prepare the database for the Steward. Idempotent; run after `alembic upgrade head`:

    cd backend
    python -m scripts.setup_steward

It connects with the platform's own credentials (POSTGRES_*, the schema owner) and:
  1. creates or updates the Steward's application role, STEWARD_DB_USER (default
     steward_app) with STEWARD_DB_PASSWORD, as NOSUPERUSER NOBYPASSRLS: row-level
     security on users' diagnostic data only holds for such a role (NFR-SEC-002);
  2. runs the Steward's own migrations (schemas steward and steward_private);
  3. grants that role what it needs, and only SELECT on the knowledge corpus, which
     only the ingest command may change (ADR-S10).
"""

import os
import re
import sys

from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from database import DATABASE_URL

ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
WRITABLE_STEWARD_TABLES = (
    "steward_tasks", "failure_events", "fault_evidence", "diagnoses", "hardware_findings",
    "proposals", "hardware_warnings", "warning_evidence",
)
KNOWLEDGE_TABLES = ("knowledge_documents", "knowledge_chunks")


def setup(role: str, password: str) -> None:
    if not ROLE_NAME.match(role):
        raise SystemExit("STEWARD_DB_USER must be a plain lower-case identifier")
    owner_url = DATABASE_URL.render_as_string(hide_password=False)
    engine = create_engine(owner_url)
    literal = password.replace("'", "''")
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(text(f"{verb} ROLE {role} LOGIN PASSWORD '{literal}' NOSUPERUSER NOBYPASSRLS"))

    cfg = Config()
    cfg.set_main_option("script_location", "prayog_steward.db:migrations")
    cfg.set_main_option("sqlalchemy.url", owner_url.replace("%", "%%"))
    previous = os.environ.get("STEWARD_APP_ROLE")
    os.environ["STEWARD_APP_ROLE"] = role
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("STEWARD_APP_ROLE", None)
        else:
            os.environ["STEWARD_APP_ROLE"] = previous

    with engine.begin() as conn:
        conn.execute(text(f"GRANT USAGE ON SCHEMA steward, steward_private TO {role}"))
        for table in WRITABLE_STEWARD_TABLES:
            conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON steward.{table} TO {role}"))
        for table in KNOWLEDGE_TABLES:
            conn.execute(text(f"REVOKE INSERT, UPDATE, DELETE ON steward.{table} FROM {role}"))
            conn.execute(text(f"GRANT SELECT ON steward.{table} TO {role}"))
        conn.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA steward_private TO {role}"
        ))
        super_or_bypass = conn.execute(
            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
    engine.dispose()
    if super_or_bypass:
        raise SystemExit(f"{role} can bypass row-level security; refusing to continue")
    print(f"Steward database ready: role {role}, schemas steward and steward_private")


def main() -> None:
    password = os.getenv("STEWARD_DB_PASSWORD")
    if not password:
        sys.exit("Set STEWARD_DB_PASSWORD (and optionally STEWARD_DB_USER) in .env first")
    setup(os.getenv("STEWARD_DB_USER", "steward_app"), password)


if __name__ == "__main__":
    main()
