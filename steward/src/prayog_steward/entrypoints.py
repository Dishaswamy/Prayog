"""Process entry points: the API server and the Worker.

Both need the platform's ports. Point ``--ports`` (or ``STEWARD_PORTS``) at a zero-argument
factory returning ``prayog_steward.container.Ports``, e.g. ``prayog.integration:steward_ports``.
The demo uses ``prayog_steward.demo:ports``.

    python -m prayog_steward.entrypoints api    --ports pkg.mod:factory [--host 0.0.0.0 --port 8000]
    python -m prayog_steward.entrypoints worker --ports pkg.mod:factory
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

from prayog_steward.config import StewardSettings
from prayog_steward.container import Ports, Steward, build
from prayog_steward.retention import purge_expired

log = logging.getLogger("prayog_steward")


def load_ports(spec: str) -> Ports:
    module_name, _, attr = spec.partition(":")
    factory = cast(Callable[[], Ports], getattr(importlib.import_module(module_name), attr))
    return factory()


def run_worker(
    steward: Steward, stop: threading.Event, purge_every: timedelta = timedelta(hours=1)
) -> None:
    """Process Steward tasks until ``stop`` is set. Safe to run several in parallel."""
    last_purge = None
    while not stop.is_set():
        try:
            worked = steward.coordinator.run_once()
        except Exception:  # database blip: back off, never die
            log.exception("worker iteration failed")
            worked = False
        now = steward.clock.now()
        if steward.maintenance_engine is not None and (
            last_purge is None or now - last_purge >= purge_every
        ):
            try:
                counts = purge_expired(
                    steward.maintenance_engine, now, steward.settings.rag_retention_period
                )
                log.info("retention purge: %s", counts)
            except Exception:
                log.exception("retention purge failed")
            last_purge = now
        if not worked:
            stop.wait(steward.settings.worker_poll_seconds)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prayog-steward")
    parser.add_argument("role", choices=["api", "worker"])
    parser.add_argument("--ports", default=os.environ.get("STEWARD_PORTS"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not args.ports:
        parser.error("--ports or STEWARD_PORTS is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    steward = build(StewardSettings(), load_ports(args.ports))
    if args.role == "worker":
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        log.info("steward worker started")
        run_worker(steward, stop)
        return

    import uvicorn

    from prayog_steward.api import create_app

    app: Any = create_app(steward)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
