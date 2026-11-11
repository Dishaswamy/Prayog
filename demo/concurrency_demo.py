r"""
Live demo: concurrent access and prevention of double allocation, against the real server.

    python demo/concurrency_demo.py --admin-email ADMIN@iitk.ac.in

Uses the student login saved by `prayog login`, and asks for the admin password (to read the pool).
Every request in a burst is released at the same instant by a barrier, like the D3 concurrency suite.

  1. Idempotency race: 8 threads submit the SAME client_request_id at once -> exactly one job.
  2. Race for the GPUs: 10 threads each ask for 4 GPUs at once. Only 16 of the 18 GPUs a
     research account may use can hold 4-GPU jobs (8 + 4 + 4), so 4 run and 6 queue.
  3. The admin pool picture is read back: every GPU unit has at most one job, and every
     running job holds exactly the number of units it asked for.

The jobs are plain GPU jobs: the fake nodes "run" each for --run-seconds (60 by default).
--cleanup cancels every job this script created at the end.
"""

import argparse
import getpass
import json
import os
import sys
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def together(n, fn):
    gate = threading.Barrier(n)

    def call(i):
        gate.wait()                                  # all n requests leave at the same moment
        try:
            return fn(i)
        except Exception as exc:                     # shown, never swallowed
            return exc
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(call, range(n)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--burst", type=int, default=10, help="parallel 4-GPU submissions (default 10)")
    parser.add_argument("--gpus", type=int, default=4)
    parser.add_argument("--cleanup", action="store_true", help="cancel this script's jobs at the end")
    parser.add_argument("--admin-password-stdin", action="store_true", help="read the admin password from stdin")
    args = parser.parse_args()

    creds = json.loads((Path(os.environ.get("PRAYOG_CONFIG_DIR", Path.home() / ".prayog")) / "credentials.json")
                       .read_text())
    server, student = creds["api_url"], {"Authorization": f"Bearer {creds['token']}"}
    http = httpx.Client(base_url=server, timeout=90)
    if http.get("/auth/me", headers=student).status_code != 200:
        sys.exit("The saved student login has expired. Run `prayog login ... --api-url " + server + "` first.")
    password = (sys.stdin.readline().rstrip("\n") if args.admin_password_stdin
                else getpass.getpass(f"Admin password for {args.admin_email}: "))
    r = http.post("/auth/login", json={"email": args.admin_email, "role": "ADMIN", "password": password})
    if r.status_code != 200:
        sys.exit(f"Admin login failed: {r.text}")
    admin = {"Authorization": f"Bearer {r.json()['access_token']}"}
    tag = uuid.uuid4().hex[:6]
    created = set()

    print(f"\n[1] Idempotency race: 8 threads submit client_request_id 'demo-{tag}' at the same instant")
    results = together(8, lambda i: http.post("/jobs", headers=student, json={
        "gpus": 1, "walltime": "00:05", "client_request_id": f"demo-{tag}"}))
    ids = sorted({x.json()["request_id"] for x in results if isinstance(x, httpx.Response) and x.status_code < 300})
    codes = sorted(str(x.status_code) if isinstance(x, httpx.Response) else type(x).__name__ for x in results)
    print(f"    responses: {', '.join(codes)}")
    print(f"    distinct jobs created: {len(ids)} -> {ids}   " + ("OK: exactly one" if len(ids) == 1 else "UNEXPECTED"))
    created.update(ids)

    print(f"\n[2] Race for the GPUs: {args.burst} threads each ask for {args.gpus} GPUs at the same instant")
    results = together(args.burst, lambda i: http.post("/jobs", headers=student, json={
        "gpus": args.gpus, "walltime": "00:05", "client_request_id": f"race-{tag}-{i}"}))
    for i, x in enumerate(results):
        if isinstance(x, httpx.Response) and x.status_code < 300:
            created.add(x.json()["request_id"])
            print(f"    thread {i:2d}: job {x.json()['request_id']} accepted")
        else:
            print(f"    thread {i:2d}: {x.status_code if isinstance(x, httpx.Response) else x}: "
                  f"{x.text[:120] if isinstance(x, httpx.Response) else ''}")

    print("\n    letting the scheduler place them...")
    time.sleep(12)
    jobs = {j["id"]: j for j in http.get("/jobs", headers=student).json() if j["id"] in created}
    by_state = defaultdict(list)
    for j in jobs.values():
        by_state[j["state"]].append(j["id"])
    for state, members in sorted(by_state.items()):
        print(f"    {state:10s} {len(members):2d}  {sorted(members)}")

    print("\n[3] The admin's pool picture: who holds each GPU unit")
    pool = http.get("/admin/pool", headers=admin, params={"audit_limit": 0}).json()
    holders = defaultdict(list)
    for node in pool["nodes"]:
        units = " ".join(f"{u['index']}:{u['job_id'] or '-'}" for u in sorted(node["units"], key=lambda u: u["index"]))
        print(f"    {node['name']:13s} {units}")
        for u in node["units"]:
            if u["job_id"] is not None:
                holders[u["job_id"]].append((node["name"], u["index"]))
    unit_owners = defaultdict(set)
    for job_id, places in holders.items():
        for place in places:
            unit_owners[place].add(job_id)
    doubled = [p for p, owners in unit_owners.items() if len(owners) > 1]
    wrong = [j for j, places in holders.items() if j in jobs and len(places) != jobs[j]["gpus"]]
    split = [j for j, places in holders.items() if len({n for n, _ in places}) > 1]
    print(f"\n    units held by two jobs:            {len(doubled)}" + ("   OK" if not doubled else f"   {doubled}"))
    print(f"    jobs holding the wrong unit count: {len(wrong)}" + ("   OK" if not wrong else f"   {wrong}"))
    print(f"    jobs split across nodes:           {len(split)}" + ("   OK" if not split else f"   {split}"))

    if args.cleanup:
        print("\nCancelling this script's jobs...")
        for job_id in sorted(created):
            http.post(f"/jobs/{job_id}/cancel", headers=student, json={})
        print(f"    cancel requested for {len(created)} job(s)")


if __name__ == "__main__":
    main()
