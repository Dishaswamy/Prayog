"""
The Steward Worker beside the API: processes S1 and S2 tasks (diagnoses, hardware
correlation) and, with a maintenance role, the hourly retention purge.

    cd backend && python -m integration.steward_worker

Any number may run (FOR UPDATE SKIP LOCKED with leases). The tick process
(python -m logic.scheduler.run) and this worker are independent: scheduling never
waits for the Steward (NFR-GPU-055).
"""

import logging
import signal
import threading

from prayog_steward.entrypoints import run_worker

from integration.steward_bridge import get_steward


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    steward = get_steward()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    logging.getLogger("prayog.steward").info("steward worker started")
    run_worker(steward, stop)


if __name__ == "__main__":  # pragma: no cover
    main()
