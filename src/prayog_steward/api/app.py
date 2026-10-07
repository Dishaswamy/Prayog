"""Steward HTTP API (researcher, S3/S4, admin) and the S3 web UI.

Security model:
- Authentication is the platform's: a bearer credential (CLI) or the platform session
  cookie (browser), resolved by the ``PrincipalResolver`` port on every request.
- Authorization decisions are server-side, per request (ADR-A7). Content routes check
  ownership or supervision and answer 404 for anything not visible (NFR-AUTH-17).
  Admin routes require the Platform Admin role and only ever return metadata
  (NFR-AUTH-18, FR-GPU-158).
- The Steward's own service principal may not call any mutating route. Each attempt is
  refused and audited (FR-GPU-151, FR-AUTH-73).
- Cookie-authenticated mutations need the ``X-Steward-CSRF`` header. Browsers cannot send
  it cross-site without a CORS preflight we never grant (CR-H).
- Errors never carry stack traces or internal detail (NFR-GPU-046, FR-STW-010).
"""

# No `from __future__ import annotations` here: FastAPI resolves the dependency aliases
# defined inside build_router from real annotations.
import logging
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from prayog_steward.container import Steward
from prayog_steward.coordinator import tasks
from prayog_steward.hardware import (
    Resolution,
    WarningNotOpen,
    close_warning,
    list_warnings,
    warning_detail,
)
from prayog_steward.owner import Conflict, Forbidden, NotFound
from prayog_steward.ports import Principal, PrincipalKind
from prayog_steward.sessions.links import AccessDenied, ActiveSession, Denial

log = logging.getLogger("prayog_steward.api")

PLATFORM_COOKIE = "prayog_session"
S3_COOKIE = "steward_s3"
CSRF_HEADER = "x-steward-csrf"
UI_DIR = Path(__file__).parent / "ui"


# ---------------------------------------------------------------- request bodies


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CloseWarningBody(Strict):
    resolution: Resolution
    reason: str = Field(min_length=1, max_length=1_000)


class AskBody(Strict):
    question: str = Field(min_length=1, max_length=2_000)
    include_attempts: list[str] = Field(default_factory=list, max_length=5)


def _json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    return jsonable_encoder(value)


# ---------------------------------------------------------------- the router


def build_router(steward: Steward) -> APIRouter:
    router = APIRouter(prefix="/steward")

    # ------------------------------------------------------------ authentication

    def _credential(request: Request) -> tuple[str | None, bool]:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip(), False
        cookie = request.cookies.get(PLATFORM_COOKIE)
        return cookie, cookie is not None

    def principal(request: Request) -> Principal:
        credential, from_cookie = _credential(request)
        resolved = steward.ports.principals.resolve(credential) if credential else None
        if resolved is None:
            raise HTTPException(401, "authentication required")
        request.state.cookie_auth = from_cookie
        return resolved

    Caller = Annotated[Principal, Depends(principal)]

    def writer(request: Request, who: Caller) -> Principal:
        """Every mutating route. The Steward service identity may never write (FR-GPU-151)."""
        if (
            who.kind is PrincipalKind.SERVICE
            or who.principal_id == steward.settings.service_principal_id
        ):
            steward.auditor.by(
                who,
                "steward.write.refused",
                request.url.path,
                outcome="refused",
                reason=f"{request.method} under a service identity",
            )
            raise HTTPException(403, "service identities may not modify records")
        if getattr(request.state, "cookie_auth", False) and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "missing CSRF header")
        return who

    Writer = Annotated[Principal, Depends(writer)]

    def admin(who: Caller) -> Principal:
        if not who.is_platform_admin:
            raise HTTPException(404, "not found")  # do not reveal admin routes exist
        return who

    Admin = Annotated[Principal, Depends(admin)]

    # ------------------------------------------------------------ S1: owner views

    @router.get("/runs/{run_id}/diagnoses")
    def run_diagnoses(run_id: str, who: Caller) -> Any:
        return _json(steward.owner.diagnoses_for_run(who, run_id))

    @router.get("/proposals")
    def my_proposals(
        who: Caller, state: Literal["PROPOSED", "APPROVED", "REJECTED"] | None = None
    ) -> Any:
        return _json(steward.owner.list_proposals(who, state))

    @router.get("/proposals/{proposal_id}")
    def one_proposal(proposal_id: uuid.UUID, who: Caller) -> Any:
        return _json(steward.owner.get_proposal(who, proposal_id))

    @router.post("/proposals/{proposal_id}/approve")
    def approve(proposal_id: uuid.UUID, who: Writer) -> Any:
        return _json(steward.owner.approve(who, proposal_id))

    @router.post("/proposals/{proposal_id}/reject")
    def reject(proposal_id: uuid.UUID, who: Writer) -> Any:
        return _json(steward.owner.reject(who, proposal_id))

    # ------------------------------------------------------------ S3: links and session

    @router.post("/attempts/{attempt_id}/link")
    def create_link(attempt_id: str, who: Writer) -> Any:
        """Called by the CLI right after a rebuild attempt reaches REBUILD_FAILED."""
        issued = steward.links.create(who, attempt_id)
        return {
            "attempt_id": issued.attempt_id,
            "url": issued.url,
            "expires_at": issued.expires_at,
            "message": (
                f"Rebuild failed. Attempt ID: {issued.attempt_id}. To analyze this error with "
                f"Steward, click here: {issued.url}"
            ),
        }

    @router.get("/s/{token}", response_class=HTMLResponse)
    def open_link(token: str, request: Request) -> Response:
        """The browser opens the link. Identity is checked before anything is shown."""
        credential, _ = _credential(request)
        who = steward.ports.principals.resolve(credential) if credential else None
        if who is None:
            return _page(401, "Please sign in to Prayog, then open the link again.")
        try:
            session = steward.links.consume(who, token)
        except AccessDenied as denied:
            return _page(403, str(denied))
        try:
            steward.s3.open(
                who, ActiveSession(session.user_id, session.attempt_id, session.expires_at)
            )
        except Exception:  # ingestion problems must not lose the session (A22)
            log.exception("steward context ingestion failed")
        response = RedirectResponse("/steward/ui/", status_code=303)
        response.set_cookie(
            S3_COOKIE,
            session.token,
            httponly=True,
            samesite="strict",
            secure=(
                steward.settings.cookie_secure
                if steward.settings.cookie_secure is not None
                else request.url.scheme == "https"
            ),
            path="/steward",
            max_age=int(steward.settings.s3_session_ttl.total_seconds()),
        )
        return response

    def s3_session(request: Request, who: Caller) -> tuple[Principal, ActiveSession]:
        return who, steward.links.resolve(who, request.cookies.get(S3_COOKIE))

    @router.get("/s3/session")
    def s3_state(request: Request, who: Caller) -> Any:
        _, session = s3_session(request, who)
        return _json(
            {
                "attempt_id": session.attempt_id,
                "expires_at": session.expires_at,
                "history": steward.s3.history(who, session),
            }
        )

    @router.post("/s3/messages")
    def s3_ask(body: AskBody, request: Request, who: Writer) -> Any:
        _, session = s3_session(request, who)
        return _json(steward.s3.ask(who, session, body.question, body.include_attempts))

    @router.post("/s3/logout")
    def s3_logout(request: Request, who: Writer) -> Response:
        token = request.cookies.get(S3_COOKIE)
        if token:
            steward.links.revoke(who, token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(S3_COOKIE, path="/steward")
        return response

    # ------------------------------------------------------------ S4

    @router.post("/attempts/{attempt_id}/optimize")
    def optimize(attempt_id: str, who: Writer) -> Any:
        return _json(steward.s4.optimize(who, attempt_id))

    # ------------------------------------------------------------ admin (metadata only)

    @router.get("/admin/warnings")
    def admin_warnings(
        who: Admin, state: Literal["OPEN", "RESOLVED", "DISMISSED"] | None = None
    ) -> Any:
        return _json(list_warnings(steward.engine, state))

    @router.get("/admin/warnings/{warning_id}")
    def admin_warning(warning_id: uuid.UUID, who: Admin) -> Any:
        view = warning_detail(
            steward.engine, warning_id, steward.ports.allocations, steward.settings
        )
        if view is None:
            raise NotFound(str(warning_id))
        return _json(view)

    @router.post("/admin/warnings/{warning_id}/close")
    def admin_close(
        warning_id: uuid.UUID, body: CloseWarningBody, request: Request, who: Admin
    ) -> Any:
        writer(request, who)
        close_warning(
            steward.engine,
            warning_id,
            admin_id=who.principal_id,
            resolution=body.resolution,
            reason=body.reason,
            now=steward.clock.now(),
        )
        steward.auditor.by(
            who, "steward.warning.close", str(warning_id), reason=body.resolution.value
        )
        return {"warning_id": str(warning_id), "state": body.resolution.value}

    @router.get("/admin/health")
    def admin_health(who: Admin) -> Any:
        """RAG pipeline and agent health as aggregates only (FR-ML-005)."""
        healthy = getattr(steward.reasoning, "healthy", None)
        health: dict[str, Any] = {
            "runtime_reachable": healthy() if callable(healthy) else None,
            "task_queue": tasks.queue_depth(steward.engine),
            "embedder": steward.embedder.name,
            "search_provider": steward.settings.search_provider,
        }
        if steward.maintenance_engine is not None:
            from sqlalchemy import text

            with steward.maintenance_engine.connect() as c:
                row = c.execute(
                    text(
                        "SELECT (SELECT count(*) FROM steward_private.rag_chunks) AS chunks,"
                        " (SELECT coalesce(sum(octet_length(body)), 0)"
                        "    FROM steward_private.rag_chunks) AS bytes,"
                        " (SELECT count(*) FROM steward_private.s3_sessions) AS sessions"
                    )
                ).one()
            health["rag"] = {"chunks": row.chunks, "bytes": row.bytes, "sessions": row.sessions}
        return health

    @router.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return router


def _page(status: int, message: str) -> HTMLResponse:
    from html import escape

    body = (
        "<!doctype html><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>Steward</title>"
        "<body style='font-family:system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>Steward</h1><p>{escape(message)}</p></body>"
    )
    return HTMLResponse(body, status_code=status)


def install_error_handlers(app: FastAPI) -> None:
    def handler(status: int, detail: str | None = None) -> Any:
        async def _h(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse({"detail": detail or str(exc) or "error"}, status_code=status)

        return _h

    app.add_exception_handler(NotFound, handler(404, "not found"))
    app.add_exception_handler(Conflict, handler(409))
    app.add_exception_handler(Forbidden, handler(403))
    app.add_exception_handler(WarningNotOpen, handler(409, "warning is not open"))
    app.add_exception_handler(ValueError, handler(400))

    async def denied(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, AccessDenied)
        status = 404 if exc.reason is Denial.NOT_FOUND else 403
        return JSONResponse({"detail": str(exc), "reason": exc.reason.value}, status_code=status)

    async def internal(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    app.add_exception_handler(AccessDenied, denied)
    app.add_exception_handler(Exception, internal)


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def _headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        # Steward link tokens travel in the URL path: never leak them via Referer.
        h.setdefault("Referrer-Policy", "no-referrer")
        path = request.url.path
        if path.startswith("/steward"):
            h.setdefault("Cache-Control", "no-store")
            if not path.startswith(("/steward/docs", "/steward/openapi")):
                h.setdefault("Content-Security-Policy", CSP)
        return response


def create_app(steward: Steward) -> FastAPI:
    """Standalone Steward service: API plus the S3 web UI under /steward/ui/."""
    app = FastAPI(
        title="Prayog Steward",
        version="0.1.0",
        docs_url="/steward/docs",
        openapi_url="/steward/openapi.json",
        redoc_url=None,
    )
    install_error_handlers(app)
    install_security_headers(app)
    app.include_router(build_router(steward))
    app.mount("/steward/ui", StaticFiles(directory=UI_DIR, html=True), name="steward-ui")
    app.state.started_at = datetime.now()
    return app
