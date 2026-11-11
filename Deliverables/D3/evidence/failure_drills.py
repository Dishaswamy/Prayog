"""Live failure/recovery drills against a real API, scheduler tick, simulated node agent and PostgreSQL.
Writes a JSON timeline to argv[1]. Run with the load venv's python; cwd = backend."""
import json, os, subprocess, sys, time
from pathlib import Path
import httpx, psycopg

OUT = Path(sys.argv[1])
SP = Path(__file__).resolve().parent
BACKEND = Path(r"C:\Users\Areen\Prayog-final\backend")
PY = r"C:\Users\Areen\Prayog-final\.venv\Scripts\python.exe"
PG_TOOL = [sys.executable, r"C:\Users\Areen\prayog-steward\tools\dev_postgres.py"]
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
DB = "prayog_d3_fail"
ENV = {**os.environ, "POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": "trust", "POSTGRES_HOST": "127.0.0.1",
       "POSTGRES_PORT": "55432", "POSTGRES_DB": DB, "JWT_SECRET_KEY": "d3-fail-only-secret-not-for-production-0123456789",
       "STEWARD_ENABLED": "false", "PRAYOG_ARTIFACT_ROOT": str(SP / "fail-artifacts"), "PYTHONPATH": str(BACKEND),
       "PRAYOG_SCHED_HEARTBEAT_TIMEOUT_SECONDS": "8", "PRAYOG_SCHED_HEARTBEAT_INTERVAL_SECONDS": "2",
       "PRAYOG_SCHED_SCHEDULER_TICK_SECONDS": "2", "PYTHONUNBUFFERED": "1"}
LOGS = SP / "fail-logs"; LOGS.mkdir(exist_ok=True)
timeline, t0 = [], time.time()

def note(event, **kw):
    row = {"t": round(time.time() - t0, 2), "event": event, **kw}; timeline.append(row); print(json.dumps(row), flush=True)

def sql(q, *a):
    with psycopg.connect(f"host=127.0.0.1 port=55432 user=postgres dbname={DB}") as c:
        return c.execute(q, a).fetchall()

def start(name, args):
    return subprocess.Popen(args, cwd=BACKEND, env=ENV, stdout=open(LOGS / f"{name}.log", "ab"), stderr=subprocess.STDOUT)

def kill(p):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True); p.wait()

def wait_for(pred, timeout, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
            if v: return v
        except Exception:
            pass
        time.sleep(step)
    return None

def api_up():
    return wait_for(lambda: httpx.get(BASE + "/docs", timeout=2).status_code == 200, 60)

def login(email, role):
    r = httpx.post(BASE + "/auth/login", json={"email": email, "password": email.split("@")[0][::-1], "role": role}, timeout=10)
    r.raise_for_status(); return {"Authorization": f"Bearer {r.json()['access_token']}"}

def job_state(jid): return sql("select state from jobs where id=%s", jid)[0][0]
def node_state(name): return sql("select state from nodes where name=%s", name)[0][0]

# ── fresh system ─────────────────────────────────────────────────────
with psycopg.connect("host=127.0.0.1 port=55432 user=postgres dbname=postgres", autocommit=True) as c:
    c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)"); c.execute(f"CREATE DATABASE {DB}")
subprocess.run([PY, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=ENV, check=True, capture_output=True)
seed = subprocess.run([PY, "-c", "from scripts.seed_dev import seed; import json; print(json.dumps(seed()))"],
                      cwd=BACKEND, env=ENV, check=True, capture_output=True, text=True)
tokens = json.loads(seed.stdout.strip().splitlines()[-1])
api = start("api", [PY, "-m", "uvicorn", "main:app", "--port", str(PORT)])
tick = start("tick", [PY, "-m", "logic.gpu.scheduler.run"])
api_up(); note("system up")
agent_args = [PY, "-m", "node_agent.simulated", "--server", BASE, "--units", "4", "--state-dir", str(SP / "fail-agent-n1"),
              "--run-seconds", "120", "--interval", "1"]
agent = start("agent", agent_args + ["--enrolment-token", tokens["n1"]])
wait_for(lambda: node_state("n1") == "AVAILABLE", 30); note("n1 AVAILABLE")
alice = login("alice@iitk.ac.in", "STUDENT")

try:
    # ── F1: node agent crashes while a job runs; reaper requeues; agent returns ──
    jid = httpx.post(BASE + "/jobs", headers=alice, json={"gpus": 2, "walltime": "00:30", "client_request_id": "f1"}).json()["request_id"]
    wait_for(lambda: job_state(jid) == "RUNNING", 40); note("F1 job running", job=jid, state=job_state(jid))
    kill(agent); k = time.time(); note("F1 agent killed (hard)")
    wait_for(lambda: node_state("n1") == "DOWN", 60); d = time.time(); note("F1 node DOWN", detect_s=round(d - k, 2))
    wait_for(lambda: job_state(jid) in ("QUEUED", "FAILED"), 30); note("F1 job after node loss", state=job_state(jid),
        release=sql("select release_reason from allocations where job_id=%s order by id desc limit 1", jid)[0][0])
    alerts = sql("select kind from alerts order by id desc limit 3") if sql("select to_regclass('alerts')")[0][0] else []
    note("F1 alerts", alerts=[a[0] for a in alerts])
    agent = start("agent", agent_args); r = time.time(); note("F1 agent restarted (same state dir)")
    wait_for(lambda: node_state("n1") == "AVAILABLE", 60); note("F1 node AVAILABLE again", after_s=round(time.time() - r, 2))
    wait_for(lambda: job_state(jid) == "RUNNING", 60); note("F1 job RUNNING again", state=job_state(jid),
        allocations=sql("select count(*) from allocations where job_id=%s", jid)[0][0], total_recovery_s=round(time.time() - k, 2))

    # ── F2: API process crashes; a client retries with the same request ID ──
    body = {"gpus": 1, "walltime": "00:30", "client_request_id": "f2-retry"}
    first = httpx.post(BASE + "/jobs", headers=alice, json=body).json()["request_id"]
    kill(api); k = time.time(); note("F2 API killed (hard)", job=first)
    try:
        httpx.post(BASE + "/jobs", headers=alice, json=body, timeout=3); refused = False
    except httpx.HTTPError as e:
        refused = type(e).__name__
    note("F2 request while down", error=refused)
    api = start("api", [PY, "-m", "uvicorn", "main:app", "--port", str(PORT)]); api_up(); note("F2 API back", downtime_s=round(time.time() - k, 2))
    retry = httpx.post(BASE + "/jobs", headers=alice, json=body)
    note("F2 retry after restart", status=retry.status_code, same_id=retry.json()["request_id"] == first,
         jobs_with_id=sql("select count(*) from jobs where client_request_id='f2-retry'")[0][0],
         token_still_valid=httpx.get(BASE + "/jobs?limit=1", headers=alice).status_code)

    # ── F3: PostgreSQL goes away under a running API, then comes back ──
    subprocess.run(PG_TOOL + ["stop"], capture_output=True); k = time.time(); note("F3 PostgreSQL stopped")
    codes = []
    for _ in range(3):
        s = time.time()
        try: codes.append((httpx.get(BASE + "/jobs?limit=1", headers=alice, timeout=20).status_code, round(time.time() - s, 2)))
        except httpx.HTTPError as e: codes.append((type(e).__name__, round(time.time() - s, 2)))
    note("F3 requests while DB down (status, seconds)", results=codes, api_alive=api.poll() is None)
    subprocess.run(PG_TOOL + ["start"], capture_output=True); note("F3 PostgreSQL restarted", down_s=round(time.time() - k, 2))
    ok = wait_for(lambda: httpx.get(BASE + "/jobs?limit=1", headers=alice, timeout=5).status_code == 200, 60)
    note("F3 API serves again without restart", ok=bool(ok), since_db_start_s=round(time.time() - k, 2), api_pid_same=api.poll() is None)
    wait_for(lambda: node_state("n1") in ("AVAILABLE", "DOWN"), 5)
    note("F3 node after DB outage", node=node_state("n1"))
    wait_for(lambda: node_state("n1") == "AVAILABLE", 60); note("F3 node AVAILABLE", node=node_state("n1"))

    # ── F4: scheduler tick process dies; event-driven passes still place jobs ──
    kill(tick); note("F4 tick process killed")
    j4 = httpx.post(BASE + "/jobs", headers=alice, json={"gpus": 1, "walltime": "00:30", "client_request_id": "f4"}).json()["request_id"]
    placed = wait_for(lambda: job_state(j4) in ("SCHEDULED", "RUNNING"), 20)
    note("F4 job placed without the tick", job=j4, state=job_state(j4), placed=bool(placed))
    kill(agent); k = time.time()
    time.sleep(14)
    note("F4 node with agent dead and no tick (no reaper)", node=node_state("n1"))
    tick = start("tick", [PY, "-m", "logic.gpu.scheduler.run"])
    wait_for(lambda: node_state("n1") == "DOWN", 30); note("F4 tick restarted; reaper catches up", node=node_state("n1"), since_kill_s=round(time.time() - k, 2))
    agent = start("agent", agent_args)
    wait_for(lambda: node_state("n1") == "AVAILABLE", 60); note("F4 agent back", node=node_state("n1"))

finally:
    for p in (agent, tick, api):
        kill(p)
OUT.write_text(json.dumps(timeline, indent=1)); note("done")
