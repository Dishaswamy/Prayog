"""
How long a scheduling pass takes, and how many SQL statements it issues (PRAYOG-109;
NFR-GPU-014), as the queue and the pool grow. Single pass, then four at once.

    python -m experiments.pass_benchmark --out pass_before.json [--profile]
"""

import argparse
import cProfile
import io
import pstats
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event, text

from database import SessionLocal, engine
from experiments.common import World, broken_invariants, summary_ms, write_results
from logic.gpu.scheduler.domain import JobState, NodeState
from logic.gpu.scheduler.passes import Trigger, run_pass

SIZES = [(40, 5, 4), (200, 10, 4), (400, 20, 4)]      # (queued jobs, nodes, units per node); the first is the design peak


class StatementCounter:
    def __init__(self):
        self.count = 0
        self.lock = threading.Lock()
        event.listen(engine, "before_cursor_execute", self._hit)

    def _hit(self, *_):
        with self.lock:
            self.count += 1


def stage(world, jobs, nodes, units):
    """A fresh queue: two labs that share with each other and have recent usage."""
    labs = [world.lab(), world.lab()]
    users = [world.user(lab)[0] for lab in labs]
    node_ids = [world.node(labs[i % 2], units=units) for i in range(nodes)]
    job_ids = [world.queued_job(users[i % 2], labs[i % 2], gpus=1 + i % 2, age_minutes=i % 90) for i in range(jobs)]
    return node_ids, job_ids


def clear(node_ids, job_ids):
    """Put everything back so the next measurement starts from the same state."""
    with SessionLocal() as session:
        session.execute(text("UPDATE gpu_units SET current_allocation_id = NULL WHERE node_id = ANY(:n)"), {"n": node_ids})
        session.execute(text("DELETE FROM scheduling_decisions WHERE job_id = ANY(:j)"), {"j": job_ids})
        session.execute(text("DELETE FROM allocation_units WHERE allocation_id IN "
                             "(SELECT id FROM allocations WHERE job_id = ANY(:j))"), {"j": job_ids})
        session.execute(text("DELETE FROM allocations WHERE job_id = ANY(:j)"), {"j": job_ids})
        session.execute(text("UPDATE jobs SET state = 'QUEUED', state_reason = NULL WHERE id = ANY(:j)"), {"j": job_ids})
        session.commit()


def retire(node_ids, job_ids):
    clear(node_ids, job_ids)
    with SessionLocal() as session:
        session.execute(text("UPDATE jobs SET state = :s WHERE id = ANY(:j)"), {"s": JobState.CANCELLED.value, "j": job_ids})
        session.execute(text("UPDATE nodes SET state = :s WHERE id = ANY(:n)"), {"s": NodeState.DOWN.value, "n": node_ids})
        session.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="pass_benchmark.json")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    world, counter = World(), StatementCounter()
    results = {}
    for jobs, nodes, units in SIZES:
        node_ids, job_ids = stage(world, jobs, nodes, units)
        single, statements, placed = [], [], []
        for _ in range(args.rounds):
            before = counter.count
            started = time.perf_counter()
            result = run_pass(SessionLocal, Trigger.TICK)
            single.append(time.perf_counter() - started)
            statements.append(counter.count - before)
            placed.append(len(result.placed))
            clear(node_ids, job_ids)

        concurrent = []
        for _ in range(args.rounds):
            gate = threading.Barrier(4)

            def one(_):
                gate.wait()
                started = time.perf_counter()
                run_pass(SessionLocal, Trigger.TICK)
                return time.perf_counter() - started

            with ThreadPoolExecutor(4) as pool:
                concurrent += list(pool.map(one, range(4)))
            clear(node_ids, job_ids)

        key = f"{jobs} queued, {nodes} nodes x {units} units"
        results[key] = {
            "single_pass_ms": summary_ms(single),
            "statements_per_pass": max(statements),
            "jobs_placed": max(placed),
            "four_concurrent_passes_ms": summary_ms(concurrent),
        }
        print(key, results[key], flush=True)

        if args.profile and (jobs, nodes) == SIZES[0][:2]:
            profiler = cProfile.Profile()
            profiler.enable()
            run_pass(SessionLocal, Trigger.TICK)
            profiler.disable()
            clear(node_ids, job_ids)
            out = io.StringIO()
            pstats.Stats(profiler, stream=out).sort_stats("cumulative").print_stats(32)
            print("\n".join(line[:130] for line in out.getvalue().splitlines() if "backend" in line or "cumtime" in line))
        retire(node_ids, job_ids)

    results["invariants_at_end"] = broken_invariants()
    write_results(args.out, results)
    world.close()


if __name__ == "__main__":
    main()
