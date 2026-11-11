"""
The concurrency experiment (PRAYOG-104; course section 12; NFR-GPU-001, 002, 005, 007, 038).

    python -m experiments.concurrency_experiment --base http://api:8000 --out results.json

Part A  Many users submit at once through the deployed API (four workers, the tick
        running) for fewer GPUs than requests. Exactly the GPUs that exist are handed out.
Part B  Passes claim the last free unit from many threads. Exactly one wins. Claim
        latency with four concurrent passes is measured against NFR-GPU-038.
Part C  The same race against five locking strategies on small tables of their own,
        to show the race is real and what each alternative costs.
"""

import argparse
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import models as orm
from database import DATABASE_URL, SessionLocal
from experiments.common import World, broken_invariants, summary_ms, wait_until, write_results
from logic.gpu.scheduler.claim import ClaimStatus, claim
from logic.gpu.scheduler.domain import JobState, NodeState, PriorityClass
from logic.gpu.scheduler.lifecycle import ReleaseReason, release
from logic.gpu.scheduler.passes import Trigger, run_pass
from logic.gpu.scheduler.scoring import ScoreBreakdown

BREAKDOWN = ScoreBreakdown(age=0.0, fair_share=0.0, contribution=0.0, size=0.0, score=0.0, escalated=False)


def at_once(count, action):
    gate = threading.Barrier(count)

    def run(i):
        gate.wait()
        return action(i)

    with ThreadPoolExecutor(count) as pool:
        return list(pool.map(run, range(count)))


def retire(node_ids, job_ids):
    """End a round: finish its jobs and take its nodes out of the way of the next round."""
    with SessionLocal() as session:
        for job_id in job_ids:
            job = session.get(orm.Job, job_id)
            if job.state == JobState.QUEUED:
                job.state = JobState.CANCELLED
        session.commit()
    for job_id in job_ids:
        with SessionLocal() as session:
            if session.get(orm.Job, job_id).state in (JobState.SCHEDULED, JobState.RUNNING):
                release(session, job_id, ReleaseReason.CANCELLED)
    with SessionLocal() as session:
        for node_id in node_ids:
            session.get(orm.Node, node_id).state = NodeState.DOWN
        session.commit()


# ── Part A ──────────────────────────────────────────────────────────────

def http_round(world, base, users, gpus, units, label):
    lab, tokens = users
    node = world.node(lab, units=units)
    ids, latencies, codes = [], [], []
    lock = threading.Lock()

    def submit(i):
        with httpx.Client(base_url=base, timeout=60) as http:
            started = time.perf_counter()
            r = http.post("/jobs", headers=tokens[i],
                          json={"gpus": gpus, "walltime": "01:00", "client_request_id": f"{label}-{uuid4().hex}"})
            elapsed = time.perf_counter() - started
        with lock:
            latencies.append(elapsed)
            codes.append(r.status_code)
            if r.status_code == 201:
                ids.append(r.json()["request_id"])

    started = time.perf_counter()
    at_once(len(tokens), submit)
    expected = units // gpus

    def placed():
        with SessionLocal() as session:
            return session.scalar(select(func.count()).select_from(orm.Job).where(
                orm.Job.id.in_(ids), orm.Job.state == JobState.SCHEDULED))

    filled = wait_until(lambda: placed() >= expected, timeout=15)
    fill_seconds = time.perf_counter() - started
    time.sleep(0.5)                                  # let any late pass finish before checking
    with SessionLocal() as session:
        scheduled = placed()
        held = session.scalar(select(func.count()).select_from(orm.GpuUnit).where(
            orm.GpuUnit.node_id == node, orm.GpuUnit.current_allocation_id.is_not(None)))
    broken = broken_invariants()
    retire([node], ids)
    return {
        "requests": len(tokens), "accepted": codes.count(201), "other_codes": sorted(set(codes) - {201}),
        "scheduled": scheduled, "expected_scheduled": expected, "units_held": held, "units_expected": expected * gpus,
        "filled_in_time": filled, "fill_seconds": round(fill_seconds, 3), "broken": broken, "latencies": latencies,
    }


def part_a(world, base, rounds):
    lab = world.lab()
    tokens = [world.user(lab)[1] for _ in range(40)]
    out = {}
    for name, count, gpus, units in (("40 users, 1 GPU each, 4 units", 40, 1, 4),
                                     ("10 users, 3 GPUs each, 4 units (all-or-nothing)", 10, 3, 4)):
        results = [http_round(world, base, (lab, tokens[:count]), gpus, units, name[:2]) for _ in range(rounds)]
        latencies = [x for r in results for x in r.pop("latencies")]
        wrong = [r for r in results if r["scheduled"] != r["expected_scheduled"] or r["units_held"] != r["units_expected"]
                 or r["accepted"] != r["requests"] or r["broken"]]
        out[name] = {
            "rounds": rounds,
            "rounds_wrong": len(wrong),
            "wrong_examples": wrong[:3],
            "double_allocations": sum(r["broken"].get("unit_with_two_live_allocations", 0) for r in results),
            "scheduled_per_round": sorted({r["scheduled"] for r in results}),
            "units_held_per_round": sorted({r["units_held"] for r in results}),
            "fill_seconds_max": max(r["fill_seconds"] for r in results),
            "submit_latency_ms": summary_ms(latencies),
        }
        print(name, {k: v for k, v in out[name].items() if k != "wrong_examples"}, flush=True)
    return out


# ── Part B ──────────────────────────────────────────────────────────────

def part_b(world, rounds):
    lab = world.lab()
    user_id = world.user(lab)[0]
    out = {}

    def race(threads):
        winners, statuses, latencies, broken_rounds = [], {}, [], 0
        for _ in range(rounds):
            node = world.node(lab, units=1)
            jobs = [world.queued_job(user_id, lab) for _ in range(threads)]

            def try_claim(i):
                with SessionLocal() as session:
                    started = time.perf_counter()
                    outcome = claim(session, jobs[i], node, PriorityClass.P0, BREAKDOWN, f"exp-{uuid4().hex[:8]}", "experiment")
                    return outcome.status, time.perf_counter() - started

            results = at_once(threads, try_claim)
            latencies += [seconds for _, seconds in results]
            won = sum(status == ClaimStatus.CLAIMED for status, _ in results)
            winners.append(won)
            for status, _ in results:
                statuses[str(status)] = statuses.get(str(status), 0) + 1
            if broken_invariants():
                broken_rounds += 1
            retire([node], jobs)
        return {
            "threads": threads, "rounds": rounds, "rounds_with_exactly_one_winner": winners.count(1),
            "outcomes": statuses, "rounds_breaking_invariants": broken_rounds, "claim_latency_ms": summary_ms(latencies),
        }

    out["four concurrent claimers (NFR-GPU-038)"] = race(4)
    out["sixteen concurrent claimers"] = race(16)
    for name, value in out.items():
        print(name, value, flush=True)
    return out


def peak_passes(world, rounds):
    """Four concurrent passes at the design peak: 40 queued jobs, 20 units on 5 nodes (ASM-02)."""
    lab_a, lab_b = world.lab(), world.lab()
    user_a, user_b = world.user(lab_a)[0], world.user(lab_b)[0]
    durations, placed_counts, broken_rounds = [], [], 0
    for _ in range(rounds):
        nodes = [world.node(lab_a if i % 2 else lab_b, units=4) for i in range(5)]
        jobs = [world.queued_job(user_a if i % 2 else user_b, lab_a if i % 2 else lab_b, gpus=1 + i % 2,
                                 age_minutes=i) for i in range(40)]

        def one_pass(_):
            started = time.perf_counter()
            result = run_pass(SessionLocal, Trigger.TICK)
            return time.perf_counter() - started, len(result.placed)

        results = at_once(4, one_pass)
        durations += [seconds for seconds, _ in results]
        placed_counts.append(sum(count for _, count in results))
        if broken_invariants():
            broken_rounds += 1
        retire(nodes, jobs)
    out = {"rounds": rounds, "pass_duration_ms": summary_ms(durations), "jobs_placed_per_round": sorted(set(placed_counts)),
           "rounds_breaking_invariants": broken_rounds}
    print("four passes at the design peak", out, flush=True)
    return out


# ── Part C ──────────────────────────────────────────────────────────────

SCHEMA = "exp_locking"


def reset_tables(conn, unique_index):
    conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    conn.execute(text(f"CREATE TABLE {SCHEMA}.units (id int PRIMARY KEY, holder int)"))
    conn.execute(text(f"CREATE TABLE {SCHEMA}.allocations (id serial PRIMARY KEY, unit_id int NOT NULL, job int NOT NULL, "
                      "released boolean NOT NULL DEFAULT false)"))
    if unique_index:
        conn.execute(text(f"CREATE UNIQUE INDEX one_live ON {SCHEMA}.allocations (unit_id) WHERE NOT released"))


def naive(conn, job):
    unit = conn.execute(text(f"SELECT id FROM {SCHEMA}.units WHERE holder IS NULL LIMIT 1")).scalar()
    if unit is None:
        return "none_free"
    conn.execute(text(f"INSERT INTO {SCHEMA}.allocations (unit_id, job) VALUES (:u, :j)"), {"u": unit, "j": job})
    conn.execute(text(f"UPDATE {SCHEMA}.units SET holder = :j WHERE id = :u"), {"u": unit, "j": job})
    return "won"


def locked(suffix):
    def strategy(conn, job):
        unit = conn.execute(text(f"SELECT id FROM {SCHEMA}.units WHERE holder IS NULL LIMIT 1 FOR UPDATE {suffix}")).scalar()
        if unit is None:
            return "none_free"
        conn.execute(text(f"INSERT INTO {SCHEMA}.allocations (unit_id, job) VALUES (:u, :j)"), {"u": unit, "j": job})
        conn.execute(text(f"UPDATE {SCHEMA}.units SET holder = :j WHERE id = :u"), {"u": unit, "j": job})
        return "won"
    return strategy


STRATEGIES = {
    "check then write, no lock, no index": dict(run=naive, unique=False, isolation="READ COMMITTED"),
    "check then write, partial unique index only": dict(run=naive, unique=True, isolation="READ COMMITTED"),
    "SERIALIZABLE transactions, no lock": dict(run=naive, unique=False, isolation="SERIALIZABLE"),
    "FOR UPDATE (waits for the lock)": dict(run=locked(""), unique=False, isolation="READ COMMITTED"),
    "FOR UPDATE SKIP LOCKED plus index (Prayog)": dict(run=locked("SKIP LOCKED"), unique=True, isolation="READ COMMITTED"),
}


def part_c(rounds, workers=8):
    out = {}
    for name, spec in STRATEGIES.items():
        engine = create_engine(DATABASE_URL, pool_size=workers + 2, max_overflow=0)
        with engine.begin() as conn:
            reset_tables(conn, spec["unique"])
        scoped = engine.execution_options(isolation_level=spec["isolation"])
        double_booked, refused, durations, wrong_empty = 0, 0, [], 0
        for _ in range(rounds):
            with engine.begin() as conn:
                conn.execute(text(f"TRUNCATE {SCHEMA}.allocations, {SCHEMA}.units"))
                conn.execute(text(f"INSERT INTO {SCHEMA}.units VALUES (1, NULL)"))

            def attempt(job):
                started = time.perf_counter()
                try:
                    with scoped.begin() as conn:
                        result = spec["run"](conn, job)
                except (IntegrityError, DBAPIError):
                    result = "refused_by_database"
                return result, time.perf_counter() - started

            results = at_once(workers, attempt)
            durations += [seconds for _, seconds in results]
            refused += sum(r == "refused_by_database" for r, _ in results)
            with engine.connect() as conn:
                live = conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.allocations WHERE NOT released")).scalar()
            double_booked += live > 1
            wrong_empty += live == 0
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {SCHEMA} CASCADE"))
        engine.dispose()
        out[name] = {
            "rounds": rounds, "workers": workers, "rounds_double_booked": double_booked,
            "rounds_with_no_winner": wrong_empty, "attempts_refused_by_database": refused,
            "mean_ms": round(statistics.mean(durations) * 1000, 2), "attempt_latency_ms": summary_ms(durations),
        }
        print(name, out[name], flush=True)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://api:8000")
    parser.add_argument("--out", default="concurrency_results.json")
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()

    world = World()
    results = {"part_a_http": part_a(world, args.base, args.rounds),
               "part_b_claim_race": part_b(world, args.rounds * 2),
               "design_peak_passes": peak_passes(world, args.rounds),
               "part_c_strategies": part_c(args.rounds * 10)}
    results["invariants_at_end"] = broken_invariants()
    world.close()
    write_results(args.out, results)
    print("invariants at end:", results["invariants_at_end"] or "all hold")


if __name__ == "__main__":
    main()
