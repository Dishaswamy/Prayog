"""Knowledge base: a shared corpus of reference papers for S3 and S4 (ADR-S10).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14

The corpus is public literature, not anyone's diagnostic data, so these tables are not
under row-level security. The application role gets SELECT only. The corpus is changed
solely by the ingest command, run with the owner or maintenance role, so a compromised
request path cannot poison what the Steward cites.
"""

from __future__ import annotations

import os
import re

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE steward.knowledge_documents (
  doc_id         uuid PRIMARY KEY,
  source         text NOT NULL UNIQUE,      -- e.g. 'arxiv:1706.02677' or 'file:<sha256>'
  title          text NOT NULL CHECK (length(btrim(title)) > 0),
  authors        text[] NOT NULL,
  url            text,
  published      date,
  content_sha256 text NOT NULL,
  ingested_at    timestamptz NOT NULL
);

CREATE TABLE steward.knowledge_chunks (
  chunk_id  uuid PRIMARY KEY,
  doc_id    uuid NOT NULL REFERENCES steward.knowledge_documents (doc_id) ON DELETE CASCADE,
  seq       integer NOT NULL CHECK (seq >= 0),
  text      text NOT NULL,
  embedder  text NOT NULL,
  embedding real[] NOT NULL CHECK (cardinality(embedding) > 0),
  UNIQUE (doc_id, embedder, seq)
);
CREATE INDEX knowledge_chunks_embedder ON steward.knowledge_chunks (embedder);
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    role = os.environ.get("STEWARD_APP_ROLE")
    if role:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", role):
            raise ValueError("STEWARD_APP_ROLE must be a plain lower-case identifier")
        op.execute(
            f"GRANT SELECT ON steward.knowledge_documents, steward.knowledge_chunks TO {role}"
        )


def downgrade() -> None:
    op.execute("DROP TABLE steward.knowledge_chunks, steward.knowledge_documents")
