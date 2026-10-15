"""
A node agent with a simulated executor (NFR-GPU-033; FR-GPU-060 to 063, 069, 077,
128, 134, 136). Runs as its own process beside the API:

    python -m node_agent.simulated --server http://localhost:8000 --units 4 \\
        --state-dir .agent-node1 --enrolment-token <token from registration>

On later starts the stored credential is used and --enrolment-token is not needed.

Each step: heartbeat (and stop any container the server says is orphaned), pull this
node's allocations, then for each one:

    new          prepare the workspace, acknowledge, and start only if the server agrees
                 (a 409 means the job was cancelled or requeued: do not start)
    running      stop it if the server asked (report STOPPED), at walltime by the agent's
                 own timer (report WALLTIME), or when the simulated process exits
                 (report EXITED with its exit code)

Started allocations are persisted before any side effect, so a repeated delivery or an
agent restart never starts one twice (FR-GPU-061). Reports carry an increasing sequence
number and are kept until delivered (FR-GPU-128).
"""

import argparse
import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("prayog.agent")


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class SimulatedExecutor:
    """Pretends to run a job: it 'exits' run_seconds after starting, with exit_code."""
    run_seconds: float = 30.0
    exit_code: int = 0

    def finished(self, started: datetime, now: datetime) -> bool:
        return now >= started + timedelta(seconds=self.run_seconds)


class SimulatedAgent:
    def __init__(
        self,
        http: httpx.Client,
        state_dir: Path,
        units: int,
        executor: SimulatedExecutor | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.http = http
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.units = units
        self.executor = executor or SimulatedExecutor()
        self.clock = clock
        self.state = self._load()

    # ── local state ────────────────────────────────────────────────────
    @property
    def _state_file(self) -> Path:
        return self.state_dir / "agent-state.json"

    def _load(self) -> dict:
        if self._state_file.exists():
            return json.loads(self._state_file.read_text())
        return {"credential": None, "node_id": None, "seq": 0, "running": {}, "done": [], "outbox": []}

    def _save(self) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self._state_file)                    # atomic: never a half-written state file

    @property
    def _headers(self) -> dict:
        return {"X-Node-Credential": self.state["credential"]}

    # ── enrolment ──────────────────────────────────────────────────────
    def enrol(self, enrolment_token: str) -> None:
        r = self.http.post("/agent/enrol", json={"enrolment_token": enrolment_token})
        r.raise_for_status()
        self.state["credential"] = r.json()["credential"]
        self.state["node_id"] = r.json()["node_id"]
        self._save()

    # ── one step ───────────────────────────────────────────────────────
    def step(self) -> None:
        self._flush_outbox()
        self._heartbeat()
        r = self.http.get("/agent/allocations", headers=self._headers)
        r.raise_for_status()
        for order in r.json():
            key = str(order["allocation_id"])
            if key in self.state["running"]:
                self._supervise(key, order)
            elif int(key) not in self.state["done"]:
                self._start(key, order)
        self._flush_outbox()

    def _heartbeat(self) -> None:
        r = self.http.post("/agent/heartbeat", headers=self._headers, json={
            "unit_count": self.units,
            "running_allocation_ids": [int(k) for k in self.state["running"]],
        })
        r.raise_for_status()
        for allocation_id in r.json()["terminate_allocation_ids"]:      # orphans after a node loss (FR-GPU-136)
            log.info("terminating orphaned allocation %s", allocation_id)
            self._cleanup(str(allocation_id))

    def _start(self, key: str, order: dict) -> None:
        workspace = self.state_dir / "workspaces" / key
        workspace.mkdir(parents=True, exist_ok=True)                    # materialise and mount (simulated)
        (workspace / "SNAPSHOT").write_text(order.get("snapshot_hash") or "")
        r = self.http.post(f"/agent/allocations/{key}/acknowledge", headers=self._headers)
        if r.status_code == 409:                                        # cancelled or requeued meanwhile
            shutil.rmtree(workspace, ignore_errors=True)
            return
        r.raise_for_status()
        # Persist before running anything: a restart must find it and never start it again.
        self.state["running"][key] = {"started": self.clock().isoformat(), "walltime_minutes": order["walltime_minutes"],
                                      "units": order["unit_indexes"]}
        self._save()
        log.info("started allocation %s on units %s", key, order["unit_indexes"])

    def _supervise(self, key: str, order: dict) -> None:
        run = self.state["running"][key]
        started = datetime.fromisoformat(run["started"])
        now = self.clock()
        if order.get("stop_reason"):
            self._finish(key, "STOPPED", None)                          # SIGTERM, stop_grace, SIGKILL (simulated)
        elif now >= started + timedelta(minutes=run["walltime_minutes"]):
            self._finish(key, "WALLTIME", None)                         # the agent's own timer (FR-GPU-134)
        elif self.executor.finished(started, now):
            self._finish(key, "EXITED", self.executor.exit_code)

    def _finish(self, key: str, outcome: str, exit_code: int | None) -> None:
        self._cleanup(key)                                              # process gone, workspace unmounted
        self.state["seq"] += 1
        self.state["outbox"].append({"allocation_id": int(key), "seq": self.state["seq"],
                                     "outcome": outcome, "exit_code": exit_code})
        self.state["done"].append(int(key))
        self._save()

    def _cleanup(self, key: str) -> None:
        shutil.rmtree(self.state_dir / "workspaces" / key, ignore_errors=True)
        self.state["running"].pop(key, None)
        self._save()

    def _flush_outbox(self) -> None:
        """Deliver buffered reports in order; stop at the first failure and retry next step (FR-GPU-128)."""
        while self.state["outbox"]:
            report = self.state["outbox"][0]
            try:
                r = self.http.post(f"/agent/allocations/{report['allocation_id']}/report", headers=self._headers,
                                   json={k: report[k] for k in ("seq", "outcome", "exit_code")})
            except httpx.HTTPError:
                return
            if r.status_code >= 500:
                return
            self.state["outbox"].pop(0)                                 # applied, a repeat, or refused for good
            self._save()


def main() -> None:  # pragma: no cover - the process wrapper
    parser = argparse.ArgumentParser(description="Prayog node agent with a simulated executor")
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--units", type=int, required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--enrolment-token")
    parser.add_argument("--run-seconds", type=float, default=30.0, help="how long each simulated job runs")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between steps")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    agent = SimulatedAgent(httpx.Client(base_url=args.server, timeout=10), Path(args.state_dir), args.units,
                           SimulatedExecutor(args.run_seconds, args.exit_code))
    if agent.state["credential"] is None:
        if not args.enrolment_token:
            parser.error("first start: --enrolment-token is required")
        agent.enrol(args.enrolment_token)
    while True:
        try:
            agent.step()
        except httpx.HTTPError as exc:
            log.warning("server unreachable: %s", exc)                   # keep going; reports stay buffered
        time.sleep(args.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
