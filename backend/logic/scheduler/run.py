"""
The safety-net tick (FR-GPU-121): a pass every scheduler_tick, in case an event
trigger was missed. Run it beside the API:

    python -m logic.scheduler.run

Tests never start it, so each event trigger is tested on its own (NFR-GPU-054).
"""

import logging
import time
from collections.abc import Callable

from sqlalchemy.orm import sessionmaker

from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.passes import Trigger, run_pass

log = logging.getLogger("prayog.scheduler")


def run_ticks(
    sessions: sessionmaker,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
    ticks: int | None = None,                     # None: forever
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    done = 0
    while ticks is None or done < ticks:
        try:
            run_pass(sessions, Trigger.TICK, cfg=cfg)
        except Exception:                         # a failed pass must not stop the tick; the next one retries
            log.exception("tick pass failed")
        done += 1
        sleep(cfg.scheduler_tick_seconds)


if __name__ == "__main__":  # pragma: no cover
    from database import SessionLocal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_ticks(SessionLocal)
