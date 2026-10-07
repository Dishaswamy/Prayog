"""Initial Steward schema: S1/S2 tables in ``steward``, S3/S4 state in ``steward_private``
under row-level security.

Revision ID: 0001
Revises:
Create Date: 2026-10-07

Raw SQL on purpose: the guarantees live in the database, not in application code.
- Append-only diagnoses and findings (NFR-GPU-057, FR-GPU-160).
- Evidence rows can only gain ``consumed_at`` (FR-GPU-163).
- A proposal is decided once and its content never changes (FR-GPU-167).
- One OPEN warning per unit signature (FR-GPU-164), and a mandatory reason to close it (FR-GPU-163).
- RLS default-deny on every private table (NFR-SEC-002, 004). With no ``app.user_id`` set,
  ``current_setting(..., true)`` is NULL and no row matches. ``user_id <> ''`` closes the
  gap where a reset setting reads as the empty string. ``user_id`` and ``attempt_id``
  are immutable (NFR-SEC-001).

Deployment note: the application must connect as a role WITHOUT superuser or BYPASSRLS,
because both bypass row-level security. The retention purge (NFR-SEC-008) arrives in
Phase 7 with its own narrowly-granted role.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

PRIVATE_TABLES = ("steward_links", "rag_chunks", "s3_messages", "response_cache")

UPGRADE = r"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS steward;
CREATE SCHEMA IF NOT EXISTS steward_private;

CREATE FUNCTION steward.forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% on %.% is not allowed: append-only', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'restrict_violation';
END $$;

-- ------------------------------------------------------------- tasks (transactional outbox)
CREATE TABLE steward.steward_tasks (
  task_id         uuid PRIMARY KEY,
  context         text NOT NULL CHECK (context IN ('S1', 'S2')),
  subject_id      text NOT NULL,
  state           text NOT NULL DEFAULT 'PENDING'
                  CHECK (state IN ('PENDING', 'RUNNING', 'DONE', 'GAVE_UP')),
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (context, subject_id)
);
CREATE INDEX steward_tasks_due ON steward.steward_tasks (next_attempt_at)
  WHERE state IN ('PENDING', 'RUNNING');

-- ------------------------------------------------------------- failure events and evidence
CREATE TABLE steward.failure_events (
  event_id         uuid PRIMARY KEY,
  run_id           text NOT NULL UNIQUE,
  job_id           text NOT NULL,
  node_id          text NOT NULL,
  unit_ids         text[] NOT NULL,
  exit_code        integer,
  failure_category text NOT NULL,
  log_refs         text[] NOT NULL,
  at               timestamptz NOT NULL
);
CREATE TRIGGER failure_events_append_only BEFORE UPDATE OR DELETE ON steward.failure_events
  FOR EACH ROW EXECUTE FUNCTION steward.forbid_change();

CREATE TABLE steward.fault_evidence (
  evidence_id       uuid PRIMARY KEY,
  run_id            text NOT NULL UNIQUE,
  job_id            text NOT NULL,
  node_id           text NOT NULL,
  unit_id           text NOT NULL,
  owner_lab         text NOT NULL,
  owner_id          text NOT NULL,
  exit_code         integer,
  failure_category  text NOT NULL,
  failure_signature text,
  at                timestamptz NOT NULL,
  consumed_at       timestamptz
);
CREATE INDEX fault_evidence_unconsumed ON steward.fault_evidence (unit_id, at)
  WHERE consumed_at IS NULL;

CREATE FUNCTION steward.evidence_only_consume() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'fault evidence cannot be deleted' USING ERRCODE = 'restrict_violation';
  END IF;
  IF OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL
     OR (to_jsonb(NEW) - 'consumed_at') <> (to_jsonb(OLD) - 'consumed_at') THEN
    RAISE EXCEPTION 'fault evidence may only be marked consumed, once'
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER fault_evidence_consume_only BEFORE UPDATE OR DELETE ON steward.fault_evidence
  FOR EACH ROW EXECUTE FUNCTION steward.evidence_only_consume();

-- ------------------------------------------------------------- diagnoses and findings (S1)
CREATE TABLE steward.diagnoses (
  diagnosis_id              uuid PRIMARY KEY,
  run_id                    text NOT NULL,
  owner_id                  text NOT NULL,
  outcome                   text NOT NULL
                            CHECK (outcome IN ('ok', 'refused', 'malformed', 'unavailable')),
  classification            text,
  claims                    jsonb NOT NULL,
  matching_earlier_failures text[] NOT NULL,
  stripped_count            integer NOT NULL CHECK (stripped_count >= 0),
  agent_id                  text NOT NULL,
  agent_version             text NOT NULL,
  model                     text,
  created_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX diagnoses_run ON steward.diagnoses (run_id, created_at);
CREATE TRIGGER diagnoses_append_only BEFORE UPDATE OR DELETE ON steward.diagnoses
  FOR EACH ROW EXECUTE FUNCTION steward.forbid_change();

CREATE TABLE steward.hardware_findings (
  finding_id   uuid PRIMARY KEY,
  diagnosis_id uuid NOT NULL REFERENCES steward.diagnoses (diagnosis_id),
  run_id       text NOT NULL,
  verdict      text NOT NULL
               CHECK (verdict IN ('likely_hardware', 'unlikely_hardware', 'inconclusive')),
  indicators   text[] NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER hardware_findings_append_only BEFORE UPDATE OR DELETE ON steward.hardware_findings
  FOR EACH ROW EXECUTE FUNCTION steward.forbid_change();

-- ------------------------------------------------------------- proposals (ST-10)
CREATE TABLE steward.proposals (
  proposal_id         uuid PRIMARY KEY,
  diagnosis_id        uuid NOT NULL REFERENCES steward.diagnoses (diagnosis_id),
  run_id              text NOT NULL,
  owner_id            text NOT NULL,
  changes             jsonb NOT NULL,
  job_spec            jsonb NOT NULL,
  requested_gpu_hours numeric(10, 2) NOT NULL CHECK (requested_gpu_hours >= 0),
  state               text NOT NULL DEFAULT 'PROPOSED'
                      CHECK (state IN ('PROPOSED', 'APPROVED', 'REJECTED')),
  decided_by          text,
  decided_at          timestamptz,
  submitted_job_id    text,
  submission_refusal  text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CHECK ((state = 'PROPOSED') = (decided_by IS NULL AND decided_at IS NULL))
);
CREATE INDEX proposals_owner_pending ON steward.proposals (owner_id) WHERE state = 'PROPOSED';

CREATE FUNCTION steward.proposal_decide_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'proposals are retained' USING ERRCODE = 'restrict_violation';
  END IF;
  IF OLD.state <> 'PROPOSED'
     OR NEW.changes <> OLD.changes OR NEW.job_spec <> OLD.job_spec
     OR NEW.requested_gpu_hours <> OLD.requested_gpu_hours
     OR NEW.run_id <> OLD.run_id OR NEW.owner_id <> OLD.owner_id
     OR NEW.diagnosis_id <> OLD.diagnosis_id THEN
    RAISE EXCEPTION 'a proposal is decided once and its content never changes'
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER proposals_decide_once BEFORE UPDATE OR DELETE ON steward.proposals
  FOR EACH ROW EXECUTE FUNCTION steward.proposal_decide_once();

-- ------------------------------------------------------------- hardware warnings (S2, ST-07)
CREATE TABLE steward.hardware_warnings (
  warning_id      uuid PRIMARY KEY,
  unit_signature  text NOT NULL,
  node_id         text NOT NULL,
  state           text NOT NULL DEFAULT 'OPEN'
                  CHECK (state IN ('OPEN', 'RESOLVED', 'DISMISSED')),
  opened_at       timestamptz NOT NULL DEFAULT now(),
  closed_at       timestamptz,
  closed_by       text,
  close_reason    text,
  steward_finding jsonb,
  CHECK ((state = 'OPEN') = (closed_at IS NULL)),
  CHECK (state = 'OPEN'
         OR (closed_by IS NOT NULL AND length(btrim(coalesce(close_reason, ''))) > 0))
);
CREATE UNIQUE INDEX hardware_warnings_one_open ON steward.hardware_warnings (unit_signature)
  WHERE state = 'OPEN';
CREATE INDEX hardware_warnings_dismissed ON steward.hardware_warnings (unit_signature, closed_at)
  WHERE state = 'DISMISSED';

CREATE TABLE steward.warning_evidence (
  warning_id  uuid NOT NULL REFERENCES steward.hardware_warnings (warning_id),
  evidence_id uuid NOT NULL REFERENCES steward.fault_evidence (evidence_id),
  PRIMARY KEY (warning_id, evidence_id)
);

-- ------------------------------------------------------------- steward_private (S3, S4)
CREATE TABLE steward_private.steward_links (
  token_hash  bytea PRIMARY KEY,
  attempt_id  text NOT NULL,
  user_id     text NOT NULL CHECK (user_id <> ''),
  session_id  text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  consumed_at timestamptz,
  CHECK (expires_at > created_at)
);

CREATE TABLE steward_private.rag_chunks (
  chunk_id   uuid PRIMARY KEY,
  user_id    text NOT NULL CHECK (user_id <> ''),
  attempt_id text NOT NULL,
  kind       text NOT NULL,
  body       text NOT NULL,
  embedding  vector(384) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_chunks_owner ON steward_private.rag_chunks (user_id, attempt_id);
CREATE INDEX rag_chunks_embedding ON steward_private.rag_chunks
  USING hnsw (embedding vector_cosine_ops);

CREATE TABLE steward_private.s3_messages (
  message_id uuid PRIMARY KEY,
  user_id    text NOT NULL CHECK (user_id <> ''),
  attempt_id text NOT NULL,
  role       text NOT NULL CHECK (role IN ('user', 'steward')),
  content    jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX s3_messages_thread ON steward_private.s3_messages (user_id, attempt_id, created_at);

CREATE TABLE steward_private.response_cache (
  cache_key  text PRIMARY KEY,
  user_id    text NOT NULL CHECK (user_id <> ''),
  attempt_id text NOT NULL,
  response   jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION steward_private.keep_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.user_id <> OLD.user_id OR NEW.attempt_id <> OLD.attempt_id THEN
    RAISE EXCEPTION 'user_id and attempt_id are immutable' USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;
"""

RLS_TEMPLATE = """
ALTER TABLE steward_private.{t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE steward_private.{t} FORCE ROW LEVEL SECURITY;
CREATE POLICY {t}_owner_only ON steward_private.{t}
  USING (user_id = current_setting('app.user_id', true))
  WITH CHECK (user_id = current_setting('app.user_id', true));
CREATE TRIGGER {t}_keep_owner BEFORE UPDATE ON steward_private.{t}
  FOR EACH ROW EXECUTE FUNCTION steward_private.keep_owner();
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    for table in PRIVATE_TABLES:
        op.execute(RLS_TEMPLATE.format(t=table))


def downgrade() -> None:
    op.execute("DROP SCHEMA steward_private CASCADE")
    op.execute(
        "DROP TABLE steward.warning_evidence, steward.hardware_warnings, steward.proposals, "
        "steward.hardware_findings, steward.diagnoses, steward.fault_evidence, "
        "steward.failure_events, steward.steward_tasks CASCADE"
    )
    op.execute(
        "DROP FUNCTION steward.forbid_change(), steward.evidence_only_consume(), "
        "steward.proposal_decide_once()"
    )
