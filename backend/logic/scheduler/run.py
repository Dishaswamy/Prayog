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

from logic.access_request import sweep_access_revocations
from logic.mlflow_release import reconcile_runs, register_mlflow_hooks
from logic.staging import expire_staged_files
from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.passes import Trigger, run_pass
from logic.scheduler.reaper import reap

log = logging.getLogger("prayog.scheduler")


def _sweep_mlflow(session) -> None:
    """ML Flow upkeep, each part on its own so one failing never stops the others or the pass."""
    for name, sweep in (("reconcile runs", reconcile_runs), ("expire staged data", expire_staged_files)):
        try:
            changed = sweep(session)
            if changed:
                log.info("%s: %s", name, changed)
        except Exception:
            session.rollback()
            log.exception("%s failed; the next tick retries", name)


def run_ticks(
    sessions: sessionmaker,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
    ticks: int | None = None,                     # None: forever
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    done = 0
    while ticks is None or done < ticks:
        try:
            with sessions() as session:
                reap(session, cfg=cfg)            # timeouts, stops and retirements first
                if sweep_access_revocations(session):   # expired students, and revokes waiting on jobs
                    log.info("ended access that was due")
                _sweep_mlflow(session)
            run_pass(sessions, Trigger.TICK, cfg=cfg)
        except Exception:                         # a failed pass must not stop the tick; the next one retries
            log.exception("tick pass failed")
        done += 1
        sleep(cfg.scheduler_tick_seconds)


if __name__ == "__main__":  # pragma: no cover
    from database import SessionLocal
    from logic.scheduler.config import load_config

    from integration.steward_bridge import register_steward_hooks

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    register_steward_hooks()        # runs this process releases reach the Steward too
    register_mlflow_hooks()         # and their ML Flow runs
    run_ticks(SessionLocal, load_config())          # an invalid configuration stops here (NFR-GPU-062)
