import argparse
import importlib
import logging
import os
import socket
import time
from uuid import uuid4

from database import SessionLocal
from logic.mlflow_outbox import dispatch_once

REQUIRED_HANDLERS = {"STEWARD_DIAGNOSIS_REQUESTED", "OPTIMIZATION_REQUESTED"}
logging.basicConfig(level=os.getenv("PRAYOG_OUTBOX_LOG_LEVEL", "INFO"))
logger = logging.getLogger("prayog.mlflow_outbox")


def _load_handlers():
    module_name = os.getenv("PRAYOG_OUTBOX_HANDLER_MODULE", "").strip()
    if not module_name:
        raise RuntimeError(
            "PRAYOG_OUTBOX_HANDLER_MODULE must point to an administrator-managed module "
            "that exports HANDLERS; refusing to claim events without configured consumers."
        )
    module = importlib.import_module(module_name)
    handlers = getattr(module, "HANDLERS", None)
    if not isinstance(handlers, dict):
        raise RuntimeError("Configured outbox handler module must export a HANDLERS dictionary.")
    missing = REQUIRED_HANDLERS.difference(handlers)
    if missing or any(not callable(handlers.get(name)) for name in REQUIRED_HANDLERS):
        raise RuntimeError(
            "Configured HANDLERS must provide callable handlers for both supported event types."
        )
    return handlers


def main():
    parser = argparse.ArgumentParser(description="Dispatch eligible ML Flow terminal events from the transactional outbox.")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum events per batch (1–100).")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")

    try:
        poll_seconds = int(os.getenv("PRAYOG_OUTBOX_POLL_SECONDS", "5"))
    except ValueError as exc:
        raise RuntimeError("PRAYOG_OUTBOX_POLL_SECONDS must be an integer between 1 and 60.") from exc
    if not 1 <= poll_seconds <= 60:
        raise RuntimeError("PRAYOG_OUTBOX_POLL_SECONDS must be between 1 and 60.")

    worker_id = os.getenv("PRAYOG_OUTBOX_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    if len(worker_id) > 80:
        worker_id = worker_id[:80]
    handlers = _load_handlers()

    while True:
        db = SessionLocal()
        batch_failed = False
        try:
            outcomes = dispatch_once(db, worker_id, handlers, limit=args.limit)
            for event_id, outcome in outcomes:
                logger.info("outbox_event_id=%s outcome=%s", event_id, outcome)
        except Exception:
            db.rollback()
            batch_failed = True
            logger.error("Outbox batch failed; event payloads and exception details were not logged.")
        finally:
            db.close()
        if args.once:
            return 1 if batch_failed else 0
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
