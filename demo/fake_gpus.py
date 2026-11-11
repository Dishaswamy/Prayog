"""
Fake GPU nodes for demoing Prayog's scheduler. Nothing is trained and no GPU is used.

Each node runs Prayog's real node agent (backend/node_agent/agent.py): enrolment, heartbeats,
pulling allocations, acknowledging, downloading and verifying code and data, shipping results,
walltime and stop handling. Only the training process is replaced by FakeRunner, which
"trains" on a timer and writes made-up metrics, checkpoints and a log. To the server, these
are ordinary nodes.

    python demo/fake_gpus.py setup --server URL --email admin@iitk.ac.in    # once: register the nodes in nodes.json
    python demo/fake_gpus.py start                                          # run all agents; Ctrl+C stops them
    python demo/fake_gpus.py kill fake-a100-8x                              # (another terminal) a node "fails"
    python demo/fake_gpus.py revive fake-a100-8x                            # and comes back

Each run can steer its fake job with hyperparameters (prayog run ... --hp KEY=VALUE):
    fake_seconds=N   how long it "trains" (default --run-seconds, 60)
    fake_steps=N     how many steps it logs (default 10)
    fake_fail=KIND   crash, with the log a real failure of that kind would leave (what the Steward diagnoses):
                       true or bug  a CUDA illegal memory access in the training code, 60% of the way through
                       oom          CUDA out of memory, 60% of the way through
                       hardware     an NVIDIA Xid "GPU has fallen off the bus" fault, 60% of the way through
                       import       a missing Python module, at start
                       data         a missing dataset file, at start
                     A number N crashes like `bug` with exit code N.

Results are deterministic: the same code, entrypoint and hyperparameters always give the same
metrics and byte-identical checkpoints, so `prayog project compare` reports a rebuild as REPRODUCED.
"""

import argparse
import getpass
import hashlib
import json
import logging
import math
import os
import random
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
DEFAULT_BACKEND = HERE.parent / "backend"       # this repo's backend
STATE = HERE / "state"

log = logging.getLogger("fake-gpus")

# fake_fail kind -> (exit code, when it fails as a share of the steps, the end of the log)
FAILURES = {
    "bug": (1, 0.6, 'Traceback (most recent call last):\n  File "main.py", line 42, in <module>\n'
                    "    loss = model(batch).loss\n"
                    "RuntimeError: CUDA error: an illegal memory access was encountered"),
    "oom": (1, 0.6, 'Traceback (most recent call last):\n  File "main.py", line 42, in <module>\n'
                    "    loss = model(batch).loss\n"
                    "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB (GPU 0; "
                    "15.78 GiB total capacity; 13.21 GiB already allocated; 1.12 GiB free)"),
    "hardware": (1, 0.6, "NVRM: Xid (PCI:0000:3b:00): 79, pid=4121, GPU has fallen off the bus.\n"
                         'Traceback (most recent call last):\n  File "main.py", line 42, in <module>\n'
                         "RuntimeError: CUDA error: unspecified launch failure"),
    "import": (1, 0.0, 'Traceback (most recent call last):\n  File "main.py", line 3, in <module>\n'
                       "    import timm\nModuleNotFoundError: No module named 'timm'"),
    "data": (1, 0.0, 'Traceback (most recent call last):\n  File "main.py", line 17, in <module>\n'
                     "FileNotFoundError: [Errno 2] No such file or directory: '/data/train/images'"),
}


def failure_of(value) -> tuple[str, int] | None:
    """The fake_fail hyperparameter as (kind, exit code), or None for a run that succeeds."""
    if value is None or value is False:
        return None
    if value is True:
        return "bug", FAILURES["bug"][0]
    if isinstance(value, int):
        return ("bug", value) if value else None
    kind = str(value).strip().lower()
    if kind not in FAILURES:
        raise RuntimeError(f"fake_fail must be true, a number or one of {', '.join(FAILURES)}, not {value!r}")
    return kind, FAILURES[kind][0]


def load_agent_module(backend: Path):
    """Prayog's node agent needs only httpx and blake3, so it is imported straight from the repo."""
    if not (backend / "node_agent" / "agent.py").is_file():
        sys.exit(f"Prayog's backend was not found at {backend}. Pass --backend PATH_TO_PRAYOG/backend.")
    sys.path.insert(0, str(backend))
    from node_agent import agent
    return agent


# ── the fake training process ──────────────────────────────────────────

class FakeRunner:
    """Stands in for DockerRunner/SubprocessRunner. The handle is plain JSON, so the agent can
    persist it and carry on after a restart."""

    def __init__(self, seconds: float, steps: int):
        self.seconds, self.steps = seconds, steps

    @staticmethod
    def _code_digest(code_dir: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(p for p in code_dir.rglob("*") if p.is_file()):
            digest.update(path.relative_to(code_dir).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def start(self, spec) -> dict:
        hp = json.loads(spec.hyperparameters_json)
        failure = failure_of(hp.get("fake_fail"))
        steps = max(1, int(hp.get("fake_steps", self.steps)))
        seed = hashlib.sha256(f"{self._code_digest(spec.code_dir)}|{spec.entrypoint}|{spec.hyperparameters_json}"
                              .encode()).hexdigest()
        handle = {
            "kind": "fake", "name": spec.name, "label": spec.label, "started": time.time(),
            "seconds": max(1.0, float(hp.get("fake_seconds", self.seconds))), "steps": steps,
            "failure": failure[0] if failure else None, "exit_code": failure[1] if failure else 0,
            "fail_at": math.ceil(steps * FAILURES[failure[0]][1]) if failure else None,
            "seed": seed, "output": str(spec.output_dir), "log": str(spec.log_path), "done": 0, "best": None,
            "gpus": list(spec.gpu_indexes),
        }
        (spec.output_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
        self._log(handle, f"[fake-gpu] {spec.label}: pretending to train on GPU(s) {handle['gpus']} "
                          f"for {handle['seconds']:g}s, {steps} steps")
        self._log(handle, f"[fake-gpu] hyperparameters: {spec.hyperparameters_json}")
        return handle

    def _log(self, handle: dict, line: str) -> None:
        with open(handle["log"], "a", encoding="utf-8") as out:
            out.write(line + "\n")

    def _write_step(self, handle: dict, step: int) -> None:
        rng = random.Random(f"{handle['seed']}:{step}")
        progress = step / handle["steps"]
        loss = round(2.3 * math.exp(-3.0 * progress) + 0.05 + rng.uniform(-0.03, 0.03), 6)
        accuracy = round(min(0.99, 0.1 + 0.85 * (1 - math.exp(-3.5 * progress)) + rng.uniform(-0.01, 0.01)), 6)
        output = Path(handle["output"])
        with open(output / "metrics.jsonl", "a", encoding="utf-8") as metrics:
            for name, value in (("loss", loss), ("accuracy", accuracy)):
                metrics.write(json.dumps({"name": name, "value": value, "step": step}) + "\n")
        best = handle["best"] is None or loss < handle["best"]
        if best:
            handle["best"] = loss
        weights = hashlib.sha256(f"{handle['seed']}:weights:{step}".encode()).digest() * 64     # 2 KB of "weights"
        tmp = output / "checkpoints" / f"step-{step}.tmp"
        tmp.write_bytes(weights)
        tmp.replace(output / "checkpoints" / (f"step-{step}-best.pt" if best else f"step-{step}.pt"))
        self._log(handle, f"step {step}/{handle['steps']}: loss={loss:.4f} accuracy={accuracy:.4f}"
                          + ("  (best so far, checkpoint saved)" if best else "  (checkpoint saved)"))

    def poll(self, handle: dict) -> int | None:
        if handle.get("stopped"):
            return 143
        elapsed = time.time() - handle["started"]
        target = min(handle["steps"], int(elapsed / handle["seconds"] * handle["steps"]))
        if handle["fail_at"] is not None:
            target = min(target, handle["fail_at"])
        while handle["done"] < target:
            handle["done"] += 1
            self._write_step(handle, handle["done"])
        if handle["fail_at"] is not None and handle["done"] >= handle["fail_at"]:
            self._log(handle, FAILURES[handle.get("failure") or "bug"][2])
            return handle["exit_code"]
        if elapsed >= handle["seconds"]:
            self._log(handle, f"[fake-gpu] finished: best loss {handle['best']}")
            return 0
        return None

    def stop(self, handle: dict) -> None:
        self._log(handle, "[fake-gpu] received SIGTERM: saving nothing more and exiting")
        handle["stopped"] = True

    def cleanup(self, handle: dict) -> None:
        pass                                    # the log is already in run.log


# One upload at a time across all fake nodes. The server's store_artifact holds a row lock on the
# experiment across an await, so two simultaneous uploads to one experiment can freeze a server
# worker for good (fixed in backend/logic/mlflow/artifacts.py). Serialising here keeps older servers safe too.
UPLOAD_LOCK = threading.Lock()


def make_agent_class(agent_module):
    class FakeNodeAgent(agent_module.NodeAgent):
        def _ship(self, key: str, final: bool) -> None:
            with UPLOAD_LOCK:
                super()._ship(key, final)

        def _complete(self, key: str) -> None:
            with UPLOAD_LOCK:
                super()._complete(key)

        def _cleanup(self, key: str) -> None:
            # The real agent makes code and data read-only, which stops Windows from deleting them.
            workspace = self.state_dir / "workspaces" / key
            for path in workspace.rglob("*") if workspace.exists() else ():
                try:
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0))
                except OSError:
                    pass
            shutil.rmtree(workspace, ignore_errors=True)
            super()._cleanup(key)
    return FakeNodeAgent


# ── setup: register the nodes as an admin ──────────────────────────────

def admin_token(http: httpx.Client, email: str, password_stdin: bool) -> str:
    password = sys.stdin.readline().rstrip("\n") if password_stdin else getpass.getpass(f"Password for {email}: ")
    r = http.post("/auth/login", json={"email": email, "password": password, "role": "ADMIN"})
    if r.status_code != 200:
        sys.exit(f"Admin login failed (HTTP {r.status_code}): {r.text}")
    return r.json()["access_token"]


def setup(args) -> None:
    layout = json.loads(Path(args.nodes).read_text(encoding="utf-8"))
    STATE.mkdir(exist_ok=True)
    config = {"server": args.server.rstrip("/")}
    (STATE / "config.json").write_text(json.dumps(config, indent=2))
    with httpx.Client(base_url=config["server"], timeout=90) as http:
        token = admin_token(http, args.email, args.password_stdin)
        headers = {"Authorization": f"Bearer {token}"}
        for node in layout:
            node_dir = STATE / node["name"]
            if (node_dir / "agent-state.json").exists() or (node_dir / "enrolment-token.txt").exists():
                print(f"  {node['name']}: already set up here, skipped")
                continue
            body = {k: node[k] for k in ("name", "owner_lab_id", "donation_mode", "teaching_pool", "unit_count")}
            r = http.post("/admin/nodes", json=body, headers=headers)
            if r.status_code != 201:
                print(f"  {node['name']}: NOT registered (HTTP {r.status_code}): {r.text}")
                continue
            node_dir.mkdir(parents=True, exist_ok=True)
            (node_dir / "enrolment-token.txt").write_text(r.json()["enrolment_token"])
            (node_dir / "node.json").write_text(json.dumps(r.json()["node"], indent=2, default=str))
            print(f"  {node['name']}: registered as node {r.json()['node']['id']} "
                  f"({node['unit_count']} GPU(s), {node['donation_mode']}"
                  f"{', teaching pool' if node['teaching_pool'] else ''}); waiting for its agent")
        http.post("/auth/logout", headers=headers)
    print("Now run:  python demo/fake_gpus.py start")


# ── start: run every node's agent ──────────────────────────────────────

def run_node(agent_class, agent_module, server: str, node_dir: Path, units: int, args, stopping: threading.Event):
    http = httpx.Client(base_url=server, timeout=60)
    runner = FakeRunner(args.run_seconds, args.steps)
    agent = agent_class(http, node_dir, units, runner,
                        executor=agent_module.SimulatedExecutor(run_seconds=args.run_seconds))
    token_file = node_dir / "enrolment-token.txt"
    while agent.state["credential"] is None and not stopping.is_set():
        try:
            agent.enrol(token_file.read_text().strip())
            token_file.unlink()
            log.info("enrolled as node %s", agent.state["node_id"])
        except httpx.HTTPStatusError as exc:
            log.error("enrolment refused (HTTP %s): %s", exc.response.status_code, exc.response.text)
            return
        except httpx.HTTPError as exc:
            log.warning("server unreachable, retrying enrolment: %s", exc)
            stopping.wait(args.interval)
    killed = False
    while not stopping.is_set():
        if (node_dir / "KILLED").exists():              # `fake_gpus.py kill NODE`: the node goes silent
            if not killed:
                log.warning("KILLED: no more heartbeats; the server marks this node DOWN in about 40 s")
            killed = True
            stopping.wait(1)
            continue
        if killed:
            log.info("revived: heartbeating again")
            killed = False
        try:
            agent.step()
        except httpx.HTTPError as exc:
            log.warning("server unreachable: %s", exc)
        except Exception:                               # keep the node alive; the demo must not die on one bad step
            log.exception("step failed")
        stopping.wait(args.interval)
    http.close()


def start(args) -> None:
    agent_module = load_agent_module(Path(args.backend))
    agent_class = make_agent_class(agent_module)
    config = json.loads((STATE / "config.json").read_text())
    layout = {n["name"]: n for n in json.loads(Path(args.nodes).read_text(encoding="utf-8"))}
    nodes = [d for d in sorted(STATE.iterdir()) if d.is_dir() and d.name in layout
             and ((d / "agent-state.json").exists() or (d / "enrolment-token.txt").exists())]
    if args.only:
        nodes = [d for d in nodes if d.name in args.only]
    if not nodes:
        sys.exit("No nodes are set up. Run `python demo/fake_gpus.py setup` first.")
    stopping = threading.Event()
    threads = []
    for node_dir in nodes:
        t = threading.Thread(target=run_node, name=node_dir.name, daemon=True,
                             args=(agent_class, agent_module, config["server"], node_dir,
                                   layout[node_dir.name]["unit_count"], args, stopping))
        t.start()
        threads.append(t)
    print(f"{len(threads)} fake node(s) running against {config['server']}: {', '.join(d.name for d in nodes)}")
    print("Ctrl+C stops them; the server marks them DOWN about 40 seconds later.")
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping the fake nodes...")
        stopping.set()
        for t in threads:
            t.join(timeout=10)


def kill_or_revive(args) -> None:
    for name in args.node:
        node_dir = STATE / name
        if not node_dir.is_dir():
            sys.exit(f"No node named {name} is set up here.")
        flag = node_dir / "KILLED"
        if args.command == "kill":
            flag.touch()
            print(f"{name}: killed (the running `start` stops its heartbeats; DOWN in about 40 s)")
        else:
            flag.unlink(missing_ok=True)
            print(f"{name}: revived (heartbeats resume; it becomes AVAILABLE again)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake GPU nodes for a Prayog scheduling demo")
    parser.add_argument("--nodes", default=str(HERE / "nodes.json"), help="the node layout (default nodes.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="register the nodes in nodes.json on the server (needs an admin login)")
    s.add_argument("--server", required=True, help="e.g. https://prayog-smoke.onrender.com")
    s.add_argument("--email", required=True, help="an ADMIN account on that server")
    s.add_argument("--password-stdin", action="store_true", help="read the password from standard input")

    r = sub.add_parser("start", help="run the agents of every node set up here")
    r.add_argument("--backend", default=str(DEFAULT_BACKEND), help="path to Prayog's backend folder")
    r.add_argument("--run-seconds", type=float, default=60.0, help="default fake training time per run")
    r.add_argument("--steps", type=int, default=10, help="default number of fake training steps per run")
    r.add_argument("--interval", type=float, default=5.0, help="seconds between agent steps (heartbeat timeout is 30)")
    r.add_argument("--only", nargs="+", metavar="NODE", help="start only these nodes")

    for name, text in (("kill", "simulate a node failure: the node stops heartbeating"),
                       ("revive", "bring a killed node back")):
        k = sub.add_parser(name, help=text)
        k.add_argument("node", nargs="+", help="node name(s), e.g. fake-a100-8x")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)-14s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    {"setup": setup, "start": start, "kill": kill_or_revive, "revive": kill_or_revive}[args.command](args)


if __name__ == "__main__":
    main()
