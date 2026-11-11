"""
The load experiment (PRAYOG-109; course section 14; NFR-GPU-038 to 041).

    python -m experiments.load_test --base http://api:8000 --out load.json

Closed-loop simulated users against the deployed API (four workers, the tick running),
with simulated agents that really acknowledge, run and report jobs so capacity turns over.
Each user runs the mix below with a think time, at increasing concurrency. The pool is the
design peak of requirement ASM-02: 20 units on 5 nodes.
"""

import argparse
import random
import threading
import time
from collections import defaultdict
from uuid import uuid4

import httpx
from sqlalchemy import text

from database import SessionLocal
from experiments.common import World, broken_invariants, summary_ms, write_results
from logic.gpu.scheduler.domain import NodeState

MIX = [("status", 45), ("list", 15), ("submit", 25), ("cancel", 10), ("quota", 5)]
TARGETS_MS = {"submit": 500, "status": 300}         # NFR-GPU-039, at the 95th percentile


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.latency = defaultdict(list)
        self.codes = defaultdict(lambda: defaultdict(int))
        self.accepted = []

    def add(self, op, seconds, code):
        with self.lock:
            self.latency[op].append(seconds)
            self.codes[op][code] += 1


def user_loop(base, headers, rec, stop, think, seed):
    rng = random.Random(seed)
    ops, weights = zip(*MIX)
    mine = []
    with httpx.Client(base_url=base, timeout=30, headers=headers) as http:
        while not stop.is_set():
            op = rng.choices(ops, weights)[0]
            if op in ("status", "cancel") and not mine:
                op = "submit"
            started = time.perf_counter()
            try:
                if op == "submit":
                    r = http.post("/jobs", json={"gpus": rng.choice((1, 1, 2)), "walltime": "00:02",
                                                 "client_request_id": uuid4().hex})
                    if r.status_code == 201:
                        mine.append(r.json()["request_id"])
                        rec.accepted.append(r.json()["request_id"])
                        mine = mine[-20:]
                elif op == "status":
                    r = http.get(f"/jobs/{rng.choice(mine)}")
                elif op == "list":
                    r = http.get("/jobs", params={"limit": 20})
                elif op == "cancel":
                    r = http.post(f"/jobs/{mine.pop(rng.randrange(len(mine)))}/cancel")
                else:
                    r = http.get("/jobs/quota")
            except httpx.HTTPError:          # a timeout or dropped connection is a result, not a crash
                rec.add(op, time.perf_counter() - started, 599)
                stop.wait(0.2)
                continue
            rec.add(op, time.perf_counter() - started, r.status_code)
            stop.wait(rng.uniform(0, 2 * think))


def agent_loop(base, credential, units, stop, run_seconds, counters):
    """A node agent: heartbeat, acknowledge what is new, report it finished after run_seconds."""
    started_at, seq = {}, 0
    headers = {"X-Node-Credential": credential}
    with httpx.Client(base_url=base, timeout=30, headers=headers) as http:
        while not stop.is_set():
            try:
                http.post("/agent/heartbeat", json={"unit_count": units, "running_allocation_ids": list(started_at)})
                for order in http.get("/agent/allocations").json():
                    allocation = order["allocation_id"]
                    if allocation not in started_at:
                        if http.post(f"/agent/allocations/{allocation}/acknowledge").status_code == 200:
                            started_at[allocation] = time.monotonic()
                            counters["acknowledged"] += 1
                    elif order.get("stop_reason"):
                        seq += 1
                        http.post(f"/agent/allocations/{allocation}/report",
                                  json={"seq": seq, "outcome": "STOPPED"})
                        del started_at[allocation]
                for allocation, begun in list(started_at.items()):
                    if time.monotonic() - begun >= run_seconds:
                        seq += 1
                        http.post(f"/agent/allocations/{allocation}/report",
                                  json={"seq": seq, "outcome": "EXITED", "exit_code": 0})
                        del started_at[allocation]
                        counters["finished"] += 1
            except httpx.HTTPError as exc:
                counters["agent_errors"] += 1
                print("agent error", type(exc).__name__, flush=True)
            stop.wait(1.0)


def time_to_scheduled(base, headers, rec, stop):
    """A probe for NFR-GPU-040: submit when a unit is free and time the job reaching SCHEDULED."""
    with httpx.Client(base_url=base, timeout=30, headers=headers) as http:
        while not stop.is_set():
            started = time.perf_counter()
            r = http.post("/jobs", json={"gpus": 1, "walltime": "00:01", "client_request_id": uuid4().hex})
            if r.status_code != 201:
                stop.wait(2)
                continue
            job = r.json()["request_id"]
            while not stop.is_set() and time.perf_counter() - started < 60:
                state = http.get(f"/jobs/{job}").json().get("state")
                if state in ("SCHEDULED", "RUNNING", "COMPLETED"):
                    rec.add("time_to_scheduled_probe", time.perf_counter() - started, 200)
                    break
                stop.wait(0.1)
            http.post(f"/jobs/{job}/cancel")
            stop.wait(2)


def seed_history(world, lab, user_id, rows):
    """Finished jobs, so the tables are not empty the way a new deployment's are."""
    with SessionLocal() as session:
        session.execute(text(
            "INSERT INTO jobs (user_id, lab_id, account_type, gpus, walltime_minutes, state, client_request_id,"
            " submitted_at, effective_submit_at) "
            "SELECT :u, :l, 'research', 1, 60, 'COMPLETED', :tag || g, now() - interval '20 days', now() - interval '20 days' "
            "FROM generate_series(1, :n) g"), {"u": user_id, "l": lab, "tag": uuid4().hex, "n": rows})
        session.commit()


def run_level(base, tokens, concurrency, seconds, think):
    rec, stop = Recorder(), threading.Event()
    threads = [threading.Thread(target=user_loop, args=(base, tokens[i % len(tokens)], rec, stop, think, i), daemon=True)
               for i in range(concurrency)]
    with SessionLocal() as session:
        passes_before = session.scalar(text("SELECT count(*) FROM scheduling_passes"))
    started = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=40)
    elapsed = time.perf_counter() - started
    with SessionLocal() as session:
        rows = session.execute(text(
            "SELECT duration_ms, claim_failures FROM scheduling_passes ORDER BY started_at DESC LIMIT :n"),
            {"n": session.scalar(text("SELECT count(*) FROM scheduling_passes")) - passes_before}).fetchall()
        found = session.scalar(text("SELECT count(*) FROM jobs WHERE id = ANY(:ids)"), {"ids": rec.accepted})
    total = sum(len(v) for v in rec.latency.values())
    server_errors = sum(n for op in rec.codes.values() for code, n in op.items() if code >= 500)
    result = {
        "users": concurrency, "seconds": round(elapsed, 1), "requests": total, "throughput_rps": round(total / elapsed, 1),
        "server_errors": server_errors,
        "jobs_accepted": len(rec.accepted),
        "jobs_accepted_but_missing": len(rec.accepted) - found,
        "by_operation": {op: {**summary_ms(v), "codes": dict(rec.codes[op])} for op, v in rec.latency.items()},
        "passes": {"count": len(rows), **{k: v for k, v in summary_ms([r[0] / 1000 for r in rows]).items() if k != "n"},
                   "claim_failures": sum(r[1] for r in rows)},
        "broken_invariants": broken_invariants(),
    }
    print(f"{concurrency:>4} users  {result['throughput_rps']:>7} req/s  errors {server_errors}  "
          + "  ".join(f"{op} p95 {s['p95']}ms" for op, s in result["by_operation"].items())
          + f"  pass p95 {result['passes'].get('p95')}ms x{len(rows)}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://api:8000")
    parser.add_argument("--out", default="load_results.json")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--levels", default="5,10,25,50,100")
    parser.add_argument("--think", type=float, default=1.0)
    parser.add_argument("--history", type=int, default=5000)
    args = parser.parse_args()

    with SessionLocal() as session:                     # earlier experiments leave nodes and jobs behind
        session.execute(text("UPDATE jobs SET state = 'CANCELLED' WHERE state = 'QUEUED'"))
        session.execute(text("UPDATE nodes SET state = :s WHERE state = 'AVAILABLE'"), {"s": NodeState.DOWN.value})
        session.commit()

    world = World()
    labs = [world.lab() for _ in range(3)]
    admin_id, admin = world.user(labs[0], role="ADMIN")
    people = [world.user(labs[i % 3]) for i in range(30)]
    tokens = [headers for _, headers in people]
    seed_history(world, labs[0], people[0][0], args.history)

    stop_agents, counters = threading.Event(), defaultdict(int)
    agents = []
    with httpx.Client(base_url=args.base, timeout=30) as http:
        for i in range(5):
            r = http.post("/admin/nodes", headers=admin, json={"name": f"load-{uuid4().hex[:8]}", "owner_lab_id": labs[i % 3],
                                                               "donation_mode": "shared", "unit_count": 4})
            r.raise_for_status()
            credential = http.post("/agent/enrol", json={"enrolment_token": r.json()["enrolment_token"]}).json()["credential"]
            agents.append(threading.Thread(target=agent_loop, args=(args.base, credential, 4, stop_agents, 20, counters), daemon=True))
    for t in agents:
        t.start()
    time.sleep(3)

    probe_rec, probe_stop = Recorder(), threading.Event()
    probe = threading.Thread(target=time_to_scheduled, args=(args.base, world.user(labs[0])[1], probe_rec, probe_stop), daemon=True)
    probe.start()

    results = {"config": vars(args), "mix": MIX, "targets_ms": TARGETS_MS, "levels": []}
    for level in (int(x) for x in args.levels.split(",")):
        results["levels"].append(run_level(args.base, tokens, level, args.seconds, args.think))

    probe_stop.set()
    probe.join(timeout=40)
    stop_agents.set()
    for t in agents:
        t.join(timeout=10)
    results["time_to_scheduled_probe_ms"] = summary_ms(probe_rec.latency["time_to_scheduled_probe"])
    results["agent_counters"] = dict(counters)
    results["invariants_at_end"] = broken_invariants()
    with SessionLocal() as session:
        results["jobs_by_state"] = dict(session.execute(text("SELECT state, count(*) FROM jobs GROUP BY state")).fetchall())
    world.close()
    write_results(args.out, results)
    print("probe", results["time_to_scheduled_probe_ms"], "agents", results["agent_counters"],
          "invariants", results["invariants_at_end"] or "all hold")


if __name__ == "__main__":
    main()
