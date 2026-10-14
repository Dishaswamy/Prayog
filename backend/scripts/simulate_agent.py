"""
Development stand-in for the node agent, which does not exist yet. It drives a placed
job through the lifecycle the agent will drive: start, then end with a report.

    cd backend
    python -m scripts.simulate_agent start 12
    python -m scripts.simulate_agent fail 12 --exit-code 1 --log-file train.log
    python -m scripts.simulate_agent complete 12
    python -m scripts.simulate_agent node-failure 12

Every end goes through lifecycle.release(), so usage is recorded and the release hooks
(the Steward's run-seal hook) run exactly as they will for the real agent.
"""

import argparse
import sys
from pathlib import Path

import models as orm
from database import SessionLocal
from logic.scheduler.domain import JobState, ReleaseReason
from logic.scheduler.lifecycle import (
    RunReport,
    mark_running,
    register_release_hook,
    release,
)
from logic.scheduler.passes import Trigger, run_pass


def _ensure_running(job_id: int) -> None:
    with SessionLocal() as session:
        job = session.get(orm.Job, job_id)
        if job is None:
            sys.exit(f"job {job_id} not found")
        if job.state == JobState.SCHEDULED:
            mark_running(session, job_id)
        elif job.state != JobState.RUNNING:
            sys.exit(f"job {job_id} is {job.state}; it must be SCHEDULED or RUNNING")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.simulate_agent")
    parser.add_argument("action", choices=["start", "fail", "complete", "node-failure"])
    parser.add_argument("job_id", type=int)
    parser.add_argument("--exit-code", type=int, default=1)
    parser.add_argument("--category", default="CRASH")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--log", help="log text, instead of --log-file")
    args = parser.parse_args(argv)

    # The same hook the API registers, so a simulated end reaches the Steward.
    from integration.steward_bridge import seal_hook
    register_release_hook(seal_hook)

    _ensure_running(args.job_id)
    if args.action == "start":
        print(f"job {args.job_id} RUNNING")
        return
    log_text = args.log_file.read_text("utf-8", errors="replace") if args.log_file else args.log
    reason, report = {
        "fail": (ReleaseReason.FAILED, RunReport(args.exit_code, args.category, log_text)),
        "complete": (ReleaseReason.COMPLETED, RunReport(0, None, log_text)),
        "node-failure": (ReleaseReason.NODE_FAILURE, RunReport(None, "NODE_FAILURE", log_text)),
    }[args.action]
    with SessionLocal() as session:
        result = release(session, args.job_id, reason, report=report)
    run_pass(SessionLocal, Trigger.RUN_END)
    print(f"job {args.job_id}: allocation {result.allocation_id} released as {reason}, "
          f"job now {result.job_state}")


if __name__ == "__main__":
    main()
