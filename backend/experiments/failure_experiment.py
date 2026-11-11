"""
The failure and recovery experiment (PRAYOG-110; course section 12; NFR-GPU-042, 043).

Run on the host beside a throwaway deployment (docker-compose.deploy.yml, project name
in --project). It really stops and kills containers, so never point it at real data.

    python backend/experiments/failure_experiment.py --base http://127.0.0.1:18000 \
        --project prayog-d3 --world world.json --out failure.json

world.json comes from: python -m experiments.seed_world (inside the API image).
"""

import argparse
import json
import subprocess
import threading
import time
from uuid import uuid4

import httpx


class Stack:
    def __init__(self, project, base):
        self.project, self.base = project, base
        self.http = httpx.Client(base_url=base, timeout=15)

    def docker(self, *args):
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)

    def container(self, service):
        return f"{self.project}-{service}-1"

    def sql(self, query):
        env = self.docker("exec", self.container("postgres"), "sh", "-c",
                          f'psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{query}"')
        return env.stdout.strip()

    def wait_healthy(self, timeout=120):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            try:
                if self.http.get("/health", timeout=3).status_code == 200:
                    return round(time.monotonic() - started, 1)
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        return None

    def invariants(self):
        checks = {
            "unit_with_two_live_allocations":
                "SELECT count(*) FROM (SELECT unit_id FROM allocation_units WHERE released_at IS NULL "
                "GROUP BY unit_id HAVING count(*) > 1) x",
            "job_with_two_live_allocations":
                "SELECT count(*) FROM (SELECT job_id FROM allocations WHERE released_at IS NULL "
                "GROUP BY job_id HAVING count(*) > 1) x",
            "placed_job_without_allocation":
                "SELECT count(*) FROM jobs j LEFT JOIN allocations a ON a.job_id = j.id AND a.released_at IS NULL "
                "WHERE j.state IN ('SCHEDULED','RUNNING') AND a.id IS NULL",
        }
        return {name: int(self.sql(q) or 0) for name, q in checks.items()}

    def isolate(self):
        """Nothing from an earlier scenario competes for this one's job."""
        self.sql("UPDATE jobs SET state='CANCELLED' WHERE state='QUEUED'")
        self.sql("UPDATE allocation_units SET released_at=now() WHERE released_at IS NULL")
        self.sql("UPDATE allocations SET released_at=now() WHERE released_at IS NULL")
        self.sql("UPDATE gpu_units SET current_allocation_id=NULL")
        self.sql("UPDATE jobs SET state='CANCELLED' WHERE state IN ('SCHEDULED','RUNNING')")
        self.sql("UPDATE nodes SET state='DECOMMISSIONED' WHERE state<>'DECOMMISSIONED'")


class Agent:
    """A node agent the experiment can pause, resume or kill."""

    def __init__(self, stack, admin, lab, units=2, interval=10):
        self.stack, self.units, self.interval = stack, units, interval      # 10 s: the production heartbeat
        r = stack.http.post("/admin/nodes", headers=admin, json={"name": f"f-{uuid4().hex[:8]}", "owner_lab_id": lab,
                                                                 "donation_mode": "shared", "unit_count": units})
        r.raise_for_status()
        self.node_id = r.json()["node"]["id"]
        enrolled = stack.http.post("/agent/enrol", json={"enrolment_token": r.json()["enrolment_token"]})
        enrolled.raise_for_status()
        self.headers = {"X-Node-Credential": enrolled.json()["credential"]}
        self.http = httpx.Client(base_url=stack.base, timeout=5, headers=self.headers)
        self.running, self.seq, self.errors, self.beats = [], 0, 0, 0
        self.alive, self.auto_ack = True, True
        self.stop = threading.Event()
        self.heartbeat()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def heartbeat(self):
        r = self.http.post("/agent/heartbeat", json={"unit_count": self.units, "running_allocation_ids": self.running})
        r.raise_for_status()
        self.beats += 1
        return r.json()

    def loop(self):
        while not self.stop.is_set():
            if self.alive:
                try:
                    self.heartbeat()
                    if self.auto_ack:
                        for order in self.http.get("/agent/allocations").json():
                            a = order["allocation_id"]
                            if a not in self.running:
                                if self.http.post(f"/agent/allocations/{a}/acknowledge").status_code == 200:
                                    self.running.append(a)
                except httpx.HTTPError:
                    self.errors += 1          # the real agent keeps its run going and keeps trying
            self.stop.wait(self.interval)

    def report(self, allocation, seq=None, **body):
        if seq is None:
            self.seq += 1
            seq = self.seq
        return self.http.post(f"/agent/allocations/{allocation}/report", json={"seq": seq, **body})

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=8)


def submit(stack, headers, request=None, gpus=1, walltime="04:00"):
    return stack.http.post("/jobs", headers=headers, json={"gpus": gpus, "walltime": walltime,
                                                           "client_request_id": request or uuid4().hex})


def state_of(stack, job):
    return stack.sql(f"SELECT state FROM jobs WHERE id={job}")


def wait_state(stack, job, wanted, timeout):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if state_of(stack, job) in wanted:
            return round(time.monotonic() - started, 1)
        time.sleep(0.5)
    return None


# ── scenarios ───────────────────────────────────────────────────────────

def database_outage(stack, world, seconds):
    """NFR-GPU-043: retryable answers, no partial record, running work keeps running, no false node failure."""
    stack.isolate()
    user = world["users"][0]
    agent = Agent(stack, world["admin"], world["lab"])
    job = submit(stack, user).json()["request_id"]
    ran = wait_state(stack, job, {"RUNNING"}, 40)
    jobs_before = stack.sql("SELECT count(*) FROM jobs")
    request = uuid4().hex

    stack.docker("stop", "--time", "1", stack.container("postgres"))
    stopped_at = time.monotonic()
    time.sleep(1)
    during = submit(stack, user, request=request)
    health = stack.http.get("/health")
    answers = []
    while time.monotonic() - stopped_at < seconds:
        answers.append(submit(stack, user, request=request).status_code)
        time.sleep(3)
    tick_alive = stack.docker("inspect", "-f", "{{.State.Running}}", stack.container("tick")).stdout.strip()

    stack.docker("start", stack.container("postgres"))
    recovered_in = stack.wait_healthy()
    retried = submit(stack, user, request=request)
    time.sleep(25)                                # two ticks: long enough for a false node failure to show
    result = {
        "outage_seconds": seconds,
        "job_was_running_before": ran is not None,
        "submit_during_outage": {"status": during.status_code, "retry_after": during.headers.get("retry-after"),
                                 "body": during.text[:160]},
        "health_during_outage": health.status_code,
        "all_answers_during_outage": sorted(set(answers)),
        "tick_process_survived": tick_alive == "true",
        "seconds_to_healthy_after_restart": recovered_in,
        "retry_with_same_request_id": retried.status_code,
        "rows_for_that_request": int(stack.sql(f"SELECT count(*) FROM jobs WHERE client_request_id='{request}'")),
        "new_job_rows": int(stack.sql("SELECT count(*) FROM jobs")) - int(jobs_before),
        "running_job_state_after": state_of(stack, job),
        "node_state_after": stack.sql(f"SELECT state FROM nodes WHERE id={agent.node_id}"),
        "node_alerts_after": stack.sql(f"SELECT count(*) FROM node_alerts WHERE node_id={agent.node_id}"),
        "uncharged_release_reason": stack.sql(f"SELECT state_reason FROM jobs WHERE id={job}"),
        "agent_errors_during_outage": agent.errors,
        "invariants": stack.invariants(),
    }
    agent.shutdown()
    return result


def api_outage(stack, world, seconds):
    """Defect D3-08: the API is away longer than the heartbeat timeout, with the tick still running."""
    stack.isolate()
    user = world["users"][6]
    agent = Agent(stack, world["admin"], world["lab"])
    job = submit(stack, user).json()["request_id"]
    wait_state(stack, job, {"RUNNING"}, 40)
    allocation = stack.sql(f"SELECT id FROM allocations WHERE job_id={job} AND released_at IS NULL")

    stack.docker("stop", "--time", "1", stack.container("api"))
    time.sleep(seconds)
    during = {"node": stack.sql(f"SELECT state FROM nodes WHERE id={agent.node_id}"), "job": state_of(stack, job)}
    stack.docker("start", stack.container("api"))
    healthy_in = stack.wait_healthy()
    time.sleep(45)                                # past the heartbeat timeout again, with the agent back
    result = {
        "outage_seconds": seconds,
        "heartbeat_timeout_seconds": 30,
        "during_outage": during,
        "seconds_to_healthy": healthy_in,
        "node_state_after": stack.sql(f"SELECT state FROM nodes WHERE id={agent.node_id}"),
        "job_state_after": state_of(stack, job),
        "same_allocation_still_live": stack.sql(
            f"SELECT released_at IS NULL FROM allocations WHERE id={allocation}") == "t",
        "false_node_down_alerts": int(stack.sql(f"SELECT count(*) FROM node_alerts WHERE node_id={agent.node_id}")),
        "invariants": stack.invariants(),
    }
    agent.shutdown()
    return result


def api_crash(stack, world):
    """NFR-GPU-042: kill -9 the API with work in every state; nothing lost, nothing duplicated."""
    stack.isolate()
    user = world["users"][1]
    agent = Agent(stack, world["admin"], world["lab"], units=2)
    running = submit(stack, user, gpus=1).json()["request_id"]
    wait_state(stack, running, {"RUNNING"}, 30)
    agent.auto_ack = False
    scheduled = submit(stack, user, gpus=1).json()["request_id"]
    wait_state(stack, scheduled, {"SCHEDULED"}, 20)
    queued = submit(stack, user, gpus=2).json()["request_id"]
    before = {j: state_of(stack, j) for j in (running, scheduled, queued)}
    count_before = stack.sql("SELECT count(*) FROM jobs")

    stack.docker("kill", stack.container("api"))
    killed_at = time.monotonic()
    failed = []
    try:
        failed.append(submit(stack, user).status_code)
    except httpx.HTTPError as exc:
        failed.append(type(exc).__name__)
    stack.docker("start", stack.container("api"))
    healthy_in = stack.wait_healthy()
    after = {j: state_of(stack, j) for j in (running, scheduled, queued)}
    orders = agent.http.get("/agent/allocations").json()
    agent.auto_ack = True
    started = wait_state(stack, scheduled, {"RUNNING"}, 30)
    result = {
        "states_before_kill": before,
        "states_after_restart": after,
        "unchanged": before == after,
        "submit_while_down": failed,
        "seconds_down": round(time.monotonic() - killed_at, 1),
        "seconds_to_healthy": healthy_in,
        "job_count_unchanged": stack.sql("SELECT count(*) FROM jobs") == count_before,
        "agent_still_sees_its_allocations": len(orders),
        "scheduled_job_started_after_restart_seconds": started,
        "invariants": stack.invariants(),
    }
    agent.shutdown()
    return result


def node_death(stack, world):
    """FR-GPU-135, 136: an agent stops mid-run. Node DOWN, job requeued with its submit time, not charged, re-placed."""
    stack.isolate()
    user = world["users"][2]
    doomed = Agent(stack, world["admin"], world["lab"])
    job = submit(stack, user).json()["request_id"]
    wait_state(stack, job, {"RUNNING"}, 30)
    submitted = stack.sql(f"SELECT effective_submit_at FROM jobs WHERE id={job}")
    usage_before = stack.sql(f"SELECT count(*) FROM usage_entries WHERE job_id={job} AND charged")

    doomed.alive = False
    died_at = time.monotonic()
    while time.monotonic() - died_at < 90:
        if stack.sql(f"SELECT state FROM nodes WHERE id={doomed.node_id}") == "DOWN":
            break
        time.sleep(0.5)
    down_after = round(time.monotonic() - died_at, 1)
    requeued = state_of(stack, job)
    spare = Agent(stack, world["admin"], world["lab"])
    replaced = wait_state(stack, job, {"RUNNING"}, 30)
    result = {
        "seconds_until_node_down": down_after,
        "bound_seconds_heartbeat_timeout_plus_tick": 40,
        "job_state_right_after_down": requeued,
        "submit_time_preserved": stack.sql(f"SELECT effective_submit_at FROM jobs WHERE id={job}") == submitted,
        "charged_usage_rows": int(stack.sql(f"SELECT count(*) FROM usage_entries WHERE job_id={job} AND charged"))
        - int(usage_before),
        "alert_raised": int(stack.sql(f"SELECT count(*) FROM node_alerts WHERE node_id={doomed.node_id}")) > 0,
        "seconds_to_run_on_a_spare_node": replaced,
        "now_on_spare_node": stack.sql(f"SELECT node_id FROM allocations WHERE job_id={job} AND released_at IS NULL")
        == str(spare.node_id),
        "invariants": stack.invariants(),
    }
    doomed.shutdown()
    spare.shutdown()
    return result


def duplicate_transactions(stack, world):
    """FR-GPU-107, 128: retries of a submit, an acknowledgement and a report take effect once."""
    stack.isolate()
    user = world["users"][3]
    agent = Agent(stack, world["admin"], world["lab"])
    agent.auto_ack = False
    request = uuid4().hex
    first, second = submit(stack, user, request=request), submit(stack, user, request=request)
    job = first.json()["request_id"]
    wait_state(stack, job, {"SCHEDULED"}, 20)
    allocation = agent.http.get("/agent/allocations").json()[0]["allocation_id"]
    acks = [agent.http.post(f"/agent/allocations/{allocation}/acknowledge").status_code for _ in range(3)]
    report = dict(seq=5, outcome="EXITED", exit_code=0)
    reports = [agent.report(allocation, **report) for _ in range(3)]
    result = {
        "submit_answers": [first.status_code, second.status_code],
        "same_request_id_returned": first.json()["request_id"] == second.json()["request_id"],
        "rows_for_request": int(stack.sql(f"SELECT count(*) FROM jobs WHERE client_request_id='{request}'")),
        "acknowledge_answers": acks,
        "runs_created": int(stack.sql(f"SELECT count(*) FROM runs WHERE job_id={job}")),
        "report_answers": [(r.status_code, r.json().get("applied")) for r in reports],
        "final_state": state_of(stack, job),
        "usage_rows": int(stack.sql(f"SELECT count(*) FROM usage_entries WHERE job_id={job}")),
        "invariants": stack.invariants(),
    }
    agent.shutdown()
    return result


def dispatch_timeout(stack, world):
    """FR-GPU-126: an agent that never acknowledges does not hold GPUs for ever."""
    stack.isolate()
    user = world["users"][4]
    agent = Agent(stack, world["admin"], world["lab"], units=1)
    agent.auto_ack = False
    job = submit(stack, user).json()["request_id"]
    wait_state(stack, job, {"SCHEDULED"}, 20)
    started = time.monotonic()
    first_allocation = stack.sql(f"SELECT id FROM allocations WHERE job_id={job} ORDER BY id LIMIT 1")
    while time.monotonic() - started < 100:
        if stack.sql(f"SELECT released_at IS NOT NULL FROM allocations WHERE id={first_allocation}") == "t":
            break
        time.sleep(0.5)
    result = {
        "seconds_until_allocation_released": round(time.monotonic() - started, 1),
        "ack_timeout_setting_seconds": 60,
        "job_state_after": state_of(stack, job),
        "reason": stack.sql(f"SELECT state_reason FROM jobs WHERE id={job}"),
        "invariants": stack.invariants(),
    }
    agent.shutdown()
    return result


def tick_crash(stack, world):
    """The safety-net tick dies: event-triggered passes carry on; once back, it picks up what was missed."""
    stack.isolate()
    user = world["users"][5]
    agent = Agent(stack, world["admin"], world["lab"])
    stack.docker("kill", stack.container("tick"))
    job = submit(stack, user).json()["request_id"]
    placed_without_tick = wait_state(stack, job, {"SCHEDULED", "RUNNING"}, 15)
    stack.docker("start", stack.container("tick"))
    time.sleep(3)
    result = {
        "job_placed_while_tick_down_seconds": placed_without_tick,
        "tick_running_again": stack.docker("inspect", "-f", "{{.State.Running}}", stack.container("tick")).stdout.strip() == "true",
        "invariants": stack.invariants(),
    }
    agent.shutdown()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18000")
    parser.add_argument("--project", default="prayog-d3")
    parser.add_argument("--world", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    world = json.loads(open(args.world, encoding="utf-8").read())
    stack = Stack(args.project, args.base)
    scenarios = {
        "duplicate_transactions": lambda: duplicate_transactions(stack, world),
        "database_outage_short_20s": lambda: database_outage(stack, world, 20),
        "database_outage_long_60s": lambda: database_outage(stack, world, 60),
        "api_outage_long_45s": lambda: api_outage(stack, world, 45),
        "api_crash": lambda: api_crash(stack, world),
        "node_death": lambda: node_death(stack, world),
        "dispatch_timeout": lambda: dispatch_timeout(stack, world),
        "tick_crash": lambda: tick_crash(stack, world),
    }
    results = {}
    for name, run in scenarios.items():
        if args.only and name not in args.only.split(","):
            continue
        print("==", name, flush=True)
        results[name] = run()
        print(json.dumps(results[name], indent=1), flush=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
