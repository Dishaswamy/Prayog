"""Retention of personal Steward state (NFR-SEC-008, A24, A35).

- ``purge_user``: on account deactivation the platform calls this. It deletes the user's
  links, sessions, RAG chunks, prompt history and cached responses. It runs as that user
  under RLS, so it can only ever touch that user's rows. Project artifacts, diagnoses and
  proposals are project records, not personal diagnostic state, and are kept.
- ``purge_expired``: deletes personal Steward state older than ``rag_retention_period``.
  It spans all users, so it needs the maintenance role (BYPASSRLS), which is used nowhere
  else.

Security audit records live in the platform's audit log and are never touched here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Engine, text

from prayog_steward.db.engine import private_tx

RLS_TABLES = ("rag_chunks", "s3_messages", "response_cache", "s3_sessions")


def purge_user(engine: Engine, user_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with private_tx(engine, user_id) as conn:
        for table in RLS_TABLES:
            rows = conn.execute(
                text(f"DELETE FROM steward_private.{table} RETURNING 1")  # RLS scopes to user
            ).all()
            counts[table] = len(rows)
        links = conn.execute(
            text("DELETE FROM steward_private.steward_links WHERE user_id = :u RETURNING 1"),
            {"u": user_id},
        ).all()
        counts["steward_links"] = len(links)
    return counts


def purge_expired(
    maintenance_engine: Engine, now: datetime, retention: timedelta
) -> dict[str, int]:
    cutoff = now - retention
    counts: dict[str, int] = {}
    with maintenance_engine.begin() as conn:
        for table in RLS_TABLES:
            rows = conn.execute(
                text(f"DELETE FROM steward_private.{table} WHERE created_at < :c RETURNING 1"),
                {"c": cutoff},
            ).all()
            counts[table] = len(rows)
        links = conn.execute(
            text("DELETE FROM steward_private.steward_links WHERE expires_at < :c RETURNING 1"),
            {"c": cutoff},
        ).all()
        counts["steward_links"] = len(links)
    return counts
