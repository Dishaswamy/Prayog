"""Process entry points: ports loading and the worker loop."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from prayog_steward.container import Ports
from prayog_steward.entrypoints import load_ports, run_worker


def test_ports_factory_is_loaded_from_an_import_string() -> None:
    assert isinstance(load_ports("prayog_steward.fakes:empty_ports"), Ports)


def test_worker_survives_errors_and_stops_on_request() -> None:
    stop = threading.Event()
    calls: list[int] = []

    def run_once() -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database blip")  # must not kill the worker
        if len(calls) >= 3:
            stop.set()
        return False

    steward: Any = SimpleNamespace(
        coordinator=SimpleNamespace(run_once=run_once),
        clock=SimpleNamespace(now=lambda: 0),
        maintenance_engine=None,
        settings=SimpleNamespace(worker_poll_seconds=0.0),
    )
    run_worker(steward, stop)
    assert len(calls) == 3
