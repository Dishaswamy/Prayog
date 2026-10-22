"""
The node agent that runs ML Flow runs for real.

    python -m node_agent.agent --server https://prayog.example --units 4 --state-dir /var/lib/prayog-agent \\
        --runner docker --image prayog-ml-runtime:python3.12 [--enrolment-token <token>]

It is the simulated agent (node_agent.simulated: heartbeats, pulling allocations, acknowledging, reporting,
the stop and walltime handling and the crash-safe state file) with one difference: an allocation that
carries a GPU run is executed instead of pretended. For such an allocation the agent

    1. acknowledges the start (a 409 means it was cancelled or requeued: do nothing),
    2. fetches the version's code and the run's dataset, verifying every byte (node_agent.workspace),
    3. starts the program (node_agent.runners) on exactly the GPUs the scheduler allocated,
    4. each step, ships new metrics and checkpoints to the server (node_agent.results),
    5. at the end, ships what is left and the log, and only then reports how it ended.

Reporting comes last on purpose: once the report releases the allocation the server refuses further
results. If the server cannot be reached the agent keeps trying for a while before it reports anyway.
An allocation with no run behind it (a plain job) is handled as the simulated agent does.
"""

import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from node_agent.results import ResultShipper
from node_agent.runners import DockerRunner, RunSpec, SubprocessRunner
from node_agent.simulated import SimulatedAgent, SimulatedExecutor
from node_agent.workspace import WorkspaceError, dump, prepare

log = logging.getLogger("prayog.agent")

START_FAILED_EXIT_CODE = 125          # the program never ran: the preparation failed
MAX_SHIP_ATTEMPTS = 8                 # steps spent trying to deliver the final results before reporting anyway


class NodeAgent(SimulatedAgent):
    def __init__(self, http: httpx.Client, state_dir: Path, units: int, runner, **kw):
        super().__init__(http, state_dir, units, **kw)
        self.runner = runner

    # ── which allocations are runs ────────────────────────────────────

    def _workload(self, key: str):
        """The run behind an allocation, or None for a plain job. Cached, so a restart asks only once."""
        known = self.state.setdefault("workloads", {})
        if key in known:
            return known[key]
        response = self.http.get(f"/agent/allocations/{key}/workload", headers=self._headers, timeout=60)
        if response.status_code == 404:
            known[key] = None
            return None
        response.raise_for_status()
        known[key] = response.json()
        return known[key]

    # ── start ──────────────────────────────────────────────────────────

    def _start(self, key: str, order: dict) -> None:
        try:
            load = self._workload(key)
        except httpx.HTTPError as exc:
            log.warning("could not read the workload of allocation %s yet: %s", key, exc)
            return
        if load is None:
            return super()._start(key, order)
        ack = self.http.post(f"/agent/allocations/{key}/acknowledge", headers=self._headers)
        if ack.status_code == 409:                                  # cancelled or requeued meanwhile
            self.state["workloads"].pop(key, None)
            return
        ack.raise_for_status()
        workspace = self.state_dir / "workspaces" / key
        workspace.mkdir(parents=True, exist_ok=True)
        entry = {"started": self.clock().isoformat(), "walltime_minutes": order["walltime_minutes"],
                 "units": order["unit_indexes"], "managed": True, "results": {}, "ship_attempts": 0}
        # Persist before running anything: a restart must find it and never start it a second time.
        self.state["running"][key] = entry
        self._save()
        try:
            dirs = prepare(self.http, self._headers, int(key), {**load, "dataset": load["dataset"]}, workspace)
            spec = RunSpec(
                name=f"alloc-{key}", run_id=load["run_id"], label=load["label"], entrypoint=load["entrypoint"],
                hyperparameters_json=dump(load), code_dir=dirs["code"], data_dir=dirs["data"],
                output_dir=dirs["output"], log_path=workspace / "run.log", gpu_indexes=tuple(order["unit_indexes"]),
            )
            entry["handle"] = self.runner.start(spec)
            log.info("started %s (allocation %s) on units %s", load["label"], key, order["unit_indexes"])
        except (WorkspaceError, httpx.HTTPError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            log.error("could not start allocation %s: %s", key, exc)
            with (workspace / "run.log").open("ab") as out:
                out.write(f"[prayog] The run could not be started: {exc}\n".encode())
            entry["finishing"] = {"outcome": "EXITED", "exit_code": START_FAILED_EXIT_CODE}
        self._save()

    # ── supervise ──────────────────────────────────────────────────────

    def _supervise(self, key: str, order: dict) -> None:
        run = self.state["running"][key]
        if not run.get("managed"):
            return super()._supervise(key, order)
        if "finishing" in run:
            return self._complete(key)
        if "handle" not in run:
            # The agent died between acknowledging and starting the program: it never ran. Fail it, visibly.
            workspace = self.state_dir / "workspaces" / key
            workspace.mkdir(parents=True, exist_ok=True)
            with (workspace / "run.log").open("ab") as out:
                out.write(b"[prayog] The run could not be started: the node agent restarted while preparing it.\n")
            run["finishing"] = {"outcome": "EXITED", "exit_code": START_FAILED_EXIT_CODE}
            self._save()
            return self._complete(key)
        started = datetime.fromisoformat(run["started"])
        handle = run["handle"]
        outcome = None
        if order.get("stop_reason"):
            outcome = ("STOPPED", None)
        elif self.clock() >= started + timedelta(minutes=run["walltime_minutes"]):
            outcome = ("WALLTIME", None)
        else:
            code = self.runner.poll(handle)
            if code is not None:
                outcome = ("EXITED", code)
        if outcome is None:
            self._ship(key, final=False)
            return
        if outcome[0] != "EXITED":
            self.runner.stop(handle)                                # SIGTERM, grace, SIGKILL
        run["finishing"] = {"outcome": outcome[0], "exit_code": outcome[1]}
        self._save()
        self._complete(key)

    def _shipper(self, key: str) -> ResultShipper:
        workspace = self.state_dir / "workspaces" / key
        return ResultShipper(self.http, self._headers, int(key), workspace / "output",
                             self.state["running"][key]["results"])

    def _ship(self, key: str, final: bool) -> None:
        try:
            self._shipper(key).ship(final)
        except httpx.HTTPError as exc:
            log.warning("could not send results of allocation %s yet: %s", key, exc)
        self._save()

    def _complete(self, key: str) -> None:
        """Deliver the final results, then report. Retries while the server is unreachable."""
        run = self.state["running"][key]
        workspace = self.state_dir / "workspaces" / key
        if run.get("handle") and not run.get("collected"):
            self.runner.cleanup(run["handle"])                      # the program's output is now in run.log
            run["collected"] = True
            self._save()
        log_tail = None
        try:
            shipper = self._shipper(key)
            shipper.ship(final=True)
            log_tail = shipper.ship_log(workspace / "run.log")
        except httpx.HTTPError as exc:
            run["ship_attempts"] += 1
            self._save()
            if run["ship_attempts"] < MAX_SHIP_ATTEMPTS:
                log.warning("final results of allocation %s not delivered (%s); will retry", key, exc)
                return
            log.error("giving up delivering the results of allocation %s", key)
        finishing = run["finishing"]
        configured, self.executor.log_text = self.executor.log_text, log_tail or None   # this run's log, not the simulated one
        try:
            self._finish(key, finishing["outcome"], finishing["exit_code"])
        finally:
            self.executor.log_text = configured
        self.state.get("workloads", {}).pop(key, None)
        self._save()


def build_runner(kind: str, image: str | None, memory: str = "16g", cpus: str = "8", gpus: bool = True):
    if kind == "docker":
        if not image:
            raise SystemExit("--image is required with --runner docker")
        return DockerRunner(image, memory=memory, cpus=cpus, expose_gpus=gpus)
    return SubprocessRunner()


def main() -> None:  # pragma: no cover - the process wrapper
    parser = argparse.ArgumentParser(description="Prayog node agent that runs ML Flow runs")
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--units", type=int, required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--enrolment-token")
    parser.add_argument("--runner", choices=["docker", "subprocess"], default="docker",
                        help="'subprocess' has NO isolation: use it only for your own code on your own machine")
    parser.add_argument("--image", help="runtime image for --runner docker; it must already exist on this node")
    parser.add_argument("--memory", default="16g", help="memory limit of each run's container")
    parser.add_argument("--cpus", default="8", help="CPU limit of each run's container (at most this node's CPUs)")
    parser.add_argument("--no-gpu-passthrough", action="store_true",
                        help="do not pass GPUs into containers (a node with no NVIDIA runtime, or a test machine)")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between steps")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    agent = NodeAgent(httpx.Client(base_url=args.server, timeout=30), Path(args.state_dir), args.units,
                      build_runner(args.runner, args.image, args.memory, args.cpus, not args.no_gpu_passthrough), executor=SimulatedExecutor())
    if agent.state["credential"] is None:
        if not args.enrolment_token:
            parser.error("first start: --enrolment-token is required")
        agent.enrol(args.enrolment_token)
    while True:
        try:
            agent.step()
        except httpx.HTTPError as exc:
            log.warning("server unreachable: %s", exc)
        time.sleep(args.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
