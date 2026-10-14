"""Run the whole Steward locally: API + UI + Worker, with fakes for the other sub-teams.

    python tools/dev_postgres.py start
    python tools/demo.py                 # rule-based reasoning, no API key needed
    python tools/demo.py --claude        # real Claude (needs ANTHROPIC_API_KEY)

Then open http://127.0.0.1:8000/demo. Pick a user, then trigger the live-demo scenarios
(Integrated Spec §20.2): a failed run diagnosed with an approvable fix, failures on one
GPU raising a hardware warning, and a failed rebuild opening the interactive session.

Development only: the /demo routes log you in by name.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import dev_postgres  # noqa: E402
import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import (  # noqa: E402
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import create_engine, make_url, text  # noqa: E402

from prayog_steward.api import create_app  # noqa: E402
from prayog_steward.clock import SystemClock  # noqa: E402
from prayog_steward.config import StewardSettings  # noqa: E402
from prayog_steward.container import Ports, Steward, build  # noqa: E402
from prayog_steward.entrypoints import run_worker  # noqa: E402
from prayog_steward.fakes import (  # noqa: E402
    FakeAllocationHistory,
    FakeAuditSink,
    FakeAuthorizer,
    FakeLedgerHistory,
    FakeLogStore,
    FakePrincipalResolver,
    FakeRebuildAttempts,
    FakeRunReader,
    FakeSubmissionPort,
)
from prayog_steward.hooks import on_run_sealed  # noqa: E402
from prayog_steward.ports import (  # noqa: E402
    AllocationRecord,
    AttemptView,
    JobSpec,
    LedgerEntry,
    Principal,
    PrincipalKind,
    RebuildState,
    RunEnd,
    RunView,
)
from steward_contracts import ReasoningPort  # noqa: E402

APP_ROLE, APP_PASSWORD = "steward_demo", "steward_demo"
USERS = {
    "alice": Principal("alice", PrincipalKind.USER, "vision", session_id="cli-alice"),
    "bob": Principal("bob", PrincipalKind.USER, "nlp", session_id="cli-bob"),
    "root": Principal(
        "root",
        PrincipalKind.USER,
        "ops",
        roles=frozenset({"platform_admin"}),
        session_id="cli-root",
    ),
}
OOM_LOG = """\
epoch 1/20 step 400/1250 loss 2.913
Traceback (most recent call last):
  File "/home/alice/vit/train.py", line 141, in <module>
    loss = model(images.cuda(), labels.cuda())
  File "/opt/venv/lib/python3.11/site-packages/torch/nn/modules/module.py", line 1532, in _call_impl
    return forward_call(*args, **kwargs)
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 3.00 GiB (GPU 0; 39.39 GiB total)
WANDB_API_KEY=0a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3
"""
REBUILD_LOG = """\
Restoring snapshot snap-7f3a ... ok
Traceback (most recent call last):
  File "/home/alice/vit/train.py", line 3, in <module>
    import timm.layers.fancy_attention
ModuleNotFoundError: No module named 'timm.layers.fancy_attention'
"""
XID_LOG = "NCCL WARN Cuda failure 'unspecified launch failure'\nXid 79: GPU has fallen off the bus"


def ensure_demo_database(dev_url: str) -> str:
    """A database of its own, so the test suite (which rebuilds schemas in the dev
    database) never wipes the demo's embedded paper corpus."""
    url = make_url(dev_url)
    server = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with server.connect() as c:
        if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = 'prayog_demo'")).scalar():
            c.execute(text("CREATE DATABASE prayog_demo"))
    server.dispose()
    return url.set(database="prayog_demo").render_as_string(hide_password=False)


DEMO_TABLES = (
    "steward.warning_evidence, steward.hardware_warnings, steward.proposals, "
    "steward.hardware_findings, steward.diagnoses, steward.fault_evidence, "
    "steward.failure_events, steward.steward_tasks, steward_private.steward_links, "
    "steward_private.s3_sessions, steward_private.rag_chunks, steward_private.s3_messages, "
    "steward_private.response_cache"
)


def prepare_database(admin_url: str) -> str:
    """Migrate to head and clear the demo's own state. The knowledge corpus is kept, so
    papers are embedded once, not on every start."""
    engine = create_engine(admin_url)
    with engine.begin() as c:
        if not c.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}
        ).scalar():
            c.execute(
                text(
                    f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}' "
                    "NOSUPERUSER NOBYPASSRLS"
                )
            )
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/prayog_steward/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", admin_url.replace("%", "%%"))
    os.environ["STEWARD_APP_ROLE"] = APP_ROLE
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DEMO_TABLES}"))
        # Grants are idempotent; they cover databases migrated before a table existed.
        c.execute(
            text(
                f"GRANT SELECT ON steward.knowledge_documents, "
                f"steward.knowledge_chunks TO {APP_ROLE}"
            )
        )
    return (
        make_url(admin_url)
        .set(username=APP_ROLE, password=APP_PASSWORD)
        .render_as_string(hide_password=False)
    )


def make_ports() -> Ports:
    now = datetime.now(UTC)
    ledger = FakeLedgerHistory(
        [
            LedgerEntry(
                "e1",
                "alice",
                "vit",
                "config",
                "snapshot snap-7f3a pins timm==0.9.2 torch==2.3.1",
                now - timedelta(days=3),
            ),
            LedgerEntry(
                "e2",
                "alice",
                "vit",
                "metrics",
                '{"val_acc": 0.812, "val_loss": 0.61, "lr": 0.0003, "epochs": 20}',
                now - timedelta(days=3),
            ),
            LedgerEntry("e9", "bob", "nlp-proj", "metrics", '{"bleu": 31.2}', now),
        ]
    )
    return Ports(
        runs=FakeRunReader(),
        logs=FakeLogStore(),
        principals=FakePrincipalResolver({f"demo-{k}": v for k, v in USERS.items()}),
        authorizer=FakeAuthorizer(project_owners={"vit": "alice", "nlp-proj": "bob"}),
        attempts=FakeRebuildAttempts(),
        submissions=FakeSubmissionPort(),
        audit=FakeAuditSink(),
        allocations=FakeAllocationHistory(),
        ledger=ledger,
    )


def make_run(
    owner: str,
    project: str,
    log_ref: str,
    *,
    unit: str,
    batch: int = 128,
    end: RunEnd = RunEnd.FAILED,
    minutes_ago: int = 1,
) -> RunView:
    now = datetime.now(UTC)
    rid = f"run-{uuid.uuid4().hex[:6]}"
    return RunView(
        run_id=rid,
        job_id=f"job-{rid[4:]}",
        project_id=project,
        owner_id=owner,
        owner_lab=USERS[owner].lab or "lab",
        node_id=unit.split("/")[0],
        unit_ids=(unit,),
        end=end,
        started_at=now - timedelta(minutes=minutes_ago + 30),
        ended_at=now - timedelta(minutes=minutes_ago),
        job_spec=JobSpec(
            "python train.py --epochs 20",
            ("torch==2.3.1", "timm==0.9.2"),
            gpu_count=2,
            walltime_minutes=120,
            image_digest="sha256:demo",
            batch_size=batch,
            precision="bf16",
        ),
        exit_code=1 if end is RunEnd.FAILED else 0,
        failure_category="CRASH" if end is RunEnd.FAILED else None,
        log_refs=(log_ref,),
    )


def demo_app(steward: Steward) -> FastAPI:
    app = create_app(steward)
    ports = steward.ports
    runs: FakeRunReader = ports.runs  # type: ignore[assignment]
    logs: FakeLogStore = ports.logs  # type: ignore[assignment]
    attempts: FakeRebuildAttempts = ports.attempts  # type: ignore[assignment]
    allocations: FakeAllocationHistory = ports.allocations  # type: ignore[assignment]

    def seal(run: RunView, log: str) -> None:
        logs.logs[run.log_refs[0]] = log
        runs.add(run)
        with steward.engine.begin() as conn:
            on_run_sealed(conn, run, settings=steward.settings, clock=steward.clock, log_tail=log)

    @app.get("/demo", response_class=HTMLResponse)
    def page(request: Request) -> str:
        who = request.cookies.get("prayog_session", "").removeprefix("demo-") or "nobody"
        return (ROOT / "tools" / "demo.html").read_text("utf-8").replace("{{who}}", who)

    @app.get("/demo/demo.js")
    def demo_js() -> Response:
        return Response(
            (ROOT / "tools" / "demo.js").read_text("utf-8"), media_type="text/javascript"
        )

    @app.get("/demo/login/{name}")
    def login(name: str) -> RedirectResponse:
        response = RedirectResponse("/demo", status_code=303)
        if name in USERS:
            response.set_cookie(
                "prayog_session", f"demo-{name}", httponly=True, samesite="lax", path="/"
            )
        return response

    @app.post("/demo/failed-run")
    def failed_run() -> JSONResponse:
        """Scenario 3: a run fails; the Steward diagnoses it and proposes a fix."""
        earlier = make_run(
            "alice",
            "vit",
            "logs/ok",
            unit="n2/gpu0",
            end=RunEnd.SUCCEEDED,
            minutes_ago=600,
            batch=64,
        )
        runs.add(earlier)
        run = make_run("alice", "vit", f"logs/{uuid.uuid4().hex}", unit="n2/gpu1")
        seal(run, OOM_LOG)
        return JSONResponse(
            {
                "run_id": run.run_id,
                "next": "GET /steward/runs/<run_id>/diagnoses and /steward/proposals as alice",
            }
        )

    @app.post("/demo/hardware")
    def hardware() -> JSONResponse:
        """Scenario 4: failures from two labs on one GPU raise a hardware warning."""
        unit = f"n7/gpu{uuid.uuid4().int % 8}"
        for owner, project in (("alice", "vit"), ("bob", "nlp-proj")):
            allocations.records.append(
                AllocationRecord(
                    unit, f"job-{owner}", USERS[owner].lab or "", datetime.now(UTC), None
                )
            )
            seal(make_run(owner, project, f"logs/{uuid.uuid4().hex}", unit=unit), XID_LOG)
        return JSONResponse({"unit": unit, "next": "log in as root and open the admin view"})

    @app.post("/demo/rebuild")
    def rebuild() -> JSONResponse:
        """A rebuild of a snapshot fails; the CLI would now print a Steward link."""
        run = make_run("alice", "vit", f"logs/{uuid.uuid4().hex}", unit="n3/gpu0")
        logs.logs[run.log_refs[0]] = REBUILD_LOG
        runs.add(run)
        attempt_id = f"att-{uuid.uuid4().hex[:6]}"
        attempts.add(
            AttemptView(attempt_id, "snap-7f3a", "vit", "alice", RebuildState.FAILED, run.run_id)
        )
        ok = make_run("alice", "vit", "logs/ok2", unit="n3/gpu1", end=RunEnd.SUCCEEDED)
        runs.add(ok)
        attempts.add(
            AttemptView(
                f"{attempt_id}-ok", "snap-7f3a", "vit", "alice", RebuildState.SUCCESS, ok.run_id
            )
        )
        issued = steward.links.create(USERS["alice"], attempt_id)
        return JSONResponse(
            {
                "attempt_id": attempt_id,
                "successful_attempt": f"{attempt_id}-ok",
                "cli_output": issued.url,
            }
        )

    @app.get("/demo/audit")
    def audit() -> JSONResponse:
        sink: FakeAuditSink = ports.audit  # type: ignore[assignment]
        return JSONResponse(
            [
                {
                    "at": e.at.isoformat(),
                    "actor": e.actor,
                    "op": e.operation,
                    "resources": e.resource_ids,
                    "outcome": e.outcome,
                    "reason": e.reason,
                }
                for e in sink.events[-60:]
            ]
        )

    return app


def seed_knowledge(steward: Steward, admin_url: str) -> None:
    """Ingest cached papers (``python -m prayog_steward.knowledge fetch ...``) if the corpus
    has none for the active embedder. Writes go through the admin URL: the app role is
    read-only on the corpus."""
    if steward.knowledge is None:
        return
    cache = ROOT / ".knowledge_cache"
    pdfs = sorted(cache.glob("*.pdf"))
    if steward.knowledge.size or not pdfs:
        print(
            f"knowledge base: {steward.knowledge.size} chunks ({steward.knowledge.embedder.name})"
        )
        return
    from prayog_steward.knowledge.store import document_from_pdf, ingest
    from prayog_steward.rag.sanitiser import Sanitiser

    writer = create_engine(admin_url)
    print(
        f"knowledge base: embedding {len(pdfs)} papers with "
        f"{steward.knowledge.embedder.name} (first start only)..."
    )
    for pdf in pdfs:
        try:
            result = ingest(
                writer,
                document_from_pdf(pdf),
                steward.knowledge.embedder,
                Sanitiser(),
                datetime.now(UTC),
            )
        except Exception as exc:  # one bad PDF must not stop the demo
            print(f"  skipped {pdf.name}: {type(exc).__name__}")
            continue
        print(f"  {result.chunks:3d} chunks  {result.title}")
    steward.knowledge.refresh_seconds = 0
    print(f"knowledge base: {steward.knowledge.size} chunks")
    steward.knowledge.refresh_seconds = 60


def reasoning(use_claude: bool) -> ReasoningPort:
    if use_claude:
        from steward_runtime.claude import ClaudeReasoning

        return ClaudeReasoning(model=os.environ.get("STEWARD_MODEL", "claude-opus-5-5"))
    from steward_runtime.heuristic import HeuristicReasoning

    return HeuristicReasoning()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude", action="store_true", help="use the real Claude API")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--http-runtime",
        action="store_true",
        help="call the Runtime over HTTP at STEWARD_RUNTIME_URL",
    )
    args = parser.parse_args()

    admin_url = os.environ.get("STEWARD_DEMO_ADMIN_URL") or ensure_demo_database(dev_postgres.URL)
    app_url = prepare_database(admin_url)
    settings = StewardSettings(
        _env_file=None,
        database_url=app_url,  # type: ignore[call-arg]
        maintenance_database_url=admin_url,
        public_base_url=f"http://127.0.0.1:{args.port}",
        task_backoff_base=timedelta(seconds=5),
        worker_poll_seconds=1.0,
    )
    if args.http_runtime:
        from prayog_steward.reasoning_client import HttpReasoning

        port: ReasoningPort = HttpReasoning(
            os.environ.get("STEWARD_RUNTIME_URL", "http://127.0.0.1:8100")
        )
    else:
        port = reasoning(args.claude)
    steward = build(settings, make_ports(), reasoning=port, clock=SystemClock())
    seed_knowledge(steward, admin_url)
    stop = threading.Event()
    threading.Thread(target=run_worker, args=(steward, stop), daemon=True).start()
    mode = "Runtime over HTTP" if args.http_runtime else ("Claude" if args.claude else "rule-based")
    print(f"Steward demo on http://{args.host}:{args.port}/demo ({mode} reasoning)")
    try:
        uvicorn.run(demo_app(steward), host=args.host, port=args.port, log_level="warning")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
