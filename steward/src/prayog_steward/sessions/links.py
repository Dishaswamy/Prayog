"""Single-use Steward links and the interactive sessions they open (FR-STW-003, 004;
NFR-SEC-003, 004, 007; UC-STW-01; A09-A11, A16, A17).

A link is a random 256-bit token. Only its SHA-256 is stored, bound to the attempt owner,
the CLI session that asked for it and the attempt, with a 15-minute expiry. Opening it
is one guarded UPDATE: unused, unexpired, and the authenticated web user is the owner.
Any other case is denied **before** any RAG context is touched, and the precise reason is
audited without payload. Consuming, expiring or replaying a link never changes the rebuild
attempt's state (§8.4).

A consumed link is exchanged for an interactive session: another random token, kept in
an HttpOnly cookie, stored hashed under row-level security and bound to (user, attempt).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NoReturn

from sqlalchemy import Engine, select, text, update

from prayog_steward.audit import Auditor
from prayog_steward.clock import Clock
from prayog_steward.config import StewardSettings
from prayog_steward.db.engine import private_tx
from prayog_steward.db.models import s3_sessions, steward_links
from prayog_steward.ports import Authorizer, Principal, RebuildAttempts, RebuildState


class Denial(StrEnum):
    NOT_FOUND = "not_found"
    WRONG_STATE = "wrong_state"
    NO_SESSION = "no_session"
    EXPIRED = "expired"  # A09 "Expired Token"
    REPLAYED = "replayed"  # A10 "Single-Use Violation"
    IDENTITY_MISMATCH = "identity_mismatch"  # A11
    UNKNOWN_TOKEN = "unknown_token"
    SESSION_ENDED = "session_ended"
    SESSION_INVALID = "session_invalid"  # A17, NFR-SEC-004


class AccessDenied(Exception):
    def __init__(self, reason: Denial, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True)
class IssuedLink:
    url: str
    attempt_id: str
    expires_at: datetime
    token: str  # returned once to the CLI; never stored or logged


@dataclass(frozen=True)
class S3Session:
    token: str
    user_id: str
    attempt_id: str
    expires_at: datetime


@dataclass(frozen=True)
class ActiveSession:
    user_id: str
    attempt_id: str
    expires_at: datetime


class LinkService:
    def __init__(
        self,
        *,
        engine: Engine,
        settings: StewardSettings,
        clock: Clock,
        attempts: RebuildAttempts,
        authorizer: Authorizer,
        auditor: Auditor,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.clock = clock
        self.attempts = attempts
        self.authorizer = authorizer
        self.auditor = auditor

    def _deny(self, principal: Principal, op: str, ref: str, reason: Denial, msg: str) -> NoReturn:
        self.auditor.by(principal, op, ref, outcome="denied", reason=reason.value)
        raise AccessDenied(reason, msg)

    def create(self, principal: Principal, attempt_id: str) -> IssuedLink:
        """Called by the CLI when an attempt reaches REBUILD_FAILED (FR-STW-003/004).
        Never for first-time runs, REBUILD_REJECTED or other states (FR-STW-002)."""
        attempt = self.attempts.get(attempt_id)
        if attempt is None or attempt.owner_id != principal.principal_id:
            self._deny(
                principal,
                "steward.link.create",
                attempt_id,
                Denial.NOT_FOUND,
                "no such rebuild attempt",
            )
        if attempt.state is not RebuildState.FAILED:
            self._deny(
                principal,
                "steward.link.create",
                attempt_id,
                Denial.WRONG_STATE,
                f"Steward diagnosis is available only for REBUILD_FAILED attempts "
                f"(this attempt is {attempt.state.value})",
            )
        if not principal.session_id or not self.authorizer.session_active(principal.session_id):
            self._deny(
                principal,
                "steward.link.create",
                attempt_id,
                Denial.NO_SESSION,
                "an active CLI session is required",
            )
        assert principal.session_id is not None
        token = secrets.token_urlsafe(32)
        now = self.clock.now()
        expires = now + self.settings.steward_link_ttl
        with self.engine.begin() as conn:
            conn.execute(
                steward_links.insert().values(
                    token_hash=token_hash(token),
                    attempt_id=attempt_id,
                    user_id=principal.principal_id,
                    session_id=principal.session_id,
                    created_at=now,
                    expires_at=expires,
                )
            )
        self.auditor.by(principal, "steward.link.create", attempt_id)
        base = self.settings.public_base_url.rstrip("/")
        return IssuedLink(f"{base}/steward/s/{token}", attempt_id, expires, token)

    def consume(self, principal: Principal, token: str) -> S3Session:
        """Open a link in the browser. Identity is checked before anything is exposed."""
        h = token_hash(token)
        now = self.clock.now()
        with self.engine.begin() as conn:
            row = conn.execute(
                update(steward_links)
                .where(
                    steward_links.c.token_hash == h,
                    steward_links.c.consumed_at.is_(None),
                    steward_links.c.expires_at > now,
                    steward_links.c.user_id == principal.principal_id,
                )
                .values(consumed_at=now)
                .returning(steward_links.c.attempt_id, steward_links.c.session_id)
            ).first()
            if row is None:
                existing = conn.execute(
                    select(steward_links).where(steward_links.c.token_hash == h)
                ).first()
        if row is None:
            if existing is None:
                reason, msg = Denial.UNKNOWN_TOKEN, "Access denied."
            elif existing.user_id != principal.principal_id:
                reason, msg = Denial.IDENTITY_MISMATCH, "Access denied."
            elif existing.consumed_at is not None:
                reason, msg = Denial.REPLAYED, "Single-Use Violation: this link was already used."
            else:
                reason, msg = Denial.EXPIRED, "Expired Token: this link has expired."
            ref = existing.attempt_id if existing is not None else "unknown"
            self._deny(principal, "steward.link.consume", ref, reason, msg)
        assert row is not None
        if not self.authorizer.session_active(row.session_id):
            self._deny(
                principal,
                "steward.link.consume",
                row.attempt_id,
                Denial.SESSION_ENDED,
                "Access denied: the session that requested this link has ended.",
            )
        attempt = self.attempts.get(row.attempt_id)
        if attempt is None or attempt.owner_id != principal.principal_id:
            self._deny(
                principal,
                "steward.link.consume",
                row.attempt_id,
                Denial.NOT_FOUND,
                "Access denied.",
            )
        session_token = secrets.token_urlsafe(32)
        expires = now + self.settings.s3_session_ttl
        with private_tx(self.engine, principal.principal_id) as conn:
            conn.execute(
                s3_sessions.insert().values(
                    session_hash=token_hash(session_token),
                    user_id=principal.principal_id,
                    attempt_id=row.attempt_id,
                    created_at=now,
                    expires_at=expires,
                )
            )
        self.auditor.by(principal, "steward.link.consume", row.attempt_id)
        return S3Session(session_token, principal.principal_id, row.attempt_id, expires)

    def resolve(self, principal: Principal, session_token: str | None) -> ActiveSession:
        """Every S3 request: the cookie's session must exist for *this* user (RLS), be
        unexpired and unrevoked (NFR-SEC-003, 004)."""
        if not session_token:
            self._deny(
                principal,
                "steward.session.use",
                "none",
                Denial.SESSION_INVALID,
                "Your Steward session is missing or has expired.",
            )
        assert session_token is not None
        now = self.clock.now()
        with private_tx(self.engine, principal.principal_id) as conn:
            row = conn.execute(
                select(s3_sessions).where(
                    s3_sessions.c.session_hash == token_hash(session_token),
                    s3_sessions.c.revoked_at.is_(None),
                    s3_sessions.c.expires_at > now,
                )
            ).first()
        if row is None:
            self._deny(
                principal,
                "steward.session.use",
                "unknown",
                Denial.SESSION_INVALID,
                "Your Steward session is missing or has expired.",
            )
        assert row is not None
        return ActiveSession(row.user_id, row.attempt_id, row.expires_at)

    def revoke(self, principal: Principal, session_token: str) -> None:
        with private_tx(self.engine, principal.principal_id) as conn:
            conn.execute(
                update(s3_sessions)
                .where(s3_sessions.c.session_hash == token_hash(session_token))
                .values(revoked_at=self.clock.now())
            )

    def purge_expired_links(self, older_than: datetime) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "DELETE FROM steward_private.steward_links WHERE expires_at < :t"
                    " RETURNING token_hash"
                ),
                {"t": older_than},
            )
            return len(result.all())
