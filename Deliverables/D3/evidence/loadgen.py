"""Closed-loop HTTP load generator: N concurrent virtual users for D seconds per scenario.
Usage: python loadgen.py BASE_URL OUT.json [label]"""
import asyncio, json, statistics, sys, time
import httpx

BASE, OUT = sys.argv[1], sys.argv[2]
LABEL = sys.argv[3] if len(sys.argv) > 3 else "run"
DUR = 15

def pct(xs, p):
    xs = sorted(xs); k = max(0, min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))); return xs[k]

async def token(c, email, role):
    r = await c.post("/auth/login", json={"email": email, "password": email.split("@")[0][::-1], "role": role})
    r.raise_for_status(); return r.json()["access_token"]

async def scenario(name, conc, make_req):
    lat, codes, stop = [], {}, time.perf_counter() + DUR
    limits = httpx.Limits(max_connections=conc, max_keepalive_connections=conc)
    async with httpx.AsyncClient(base_url=BASE, timeout=60, limits=limits) as c:
        ctx = await make_req(c, None)  # setup
        async def vu(i):
            n = 0
            while time.perf_counter() < stop:
                t = time.perf_counter()
                try:
                    r = await make_req(c, (ctx, i, n)); code = r.status_code
                except httpx.HTTPError as e:
                    code = type(e).__name__
                n += 1
                lat.append((time.perf_counter() - t) * 1000); codes[code] = codes.get(code, 0) + 1
        t0 = time.perf_counter(); await asyncio.gather(*(vu(i) for i in range(conc))); el = time.perf_counter() - t0
    ok = sum(v for k, v in codes.items() if isinstance(k, int) and k < 400)
    res = dict(scenario=name, concurrency=conc, requests=len(lat), seconds=round(el, 2), rps=round(len(lat) / el, 1),
               p50_ms=round(pct(lat, 50), 1), p95_ms=round(pct(lat, 95), 1), p99_ms=round(pct(lat, 99), 1),
               max_ms=round(max(lat), 1), mean_ms=round(statistics.mean(lat), 1), error_rate=round(1 - ok / len(lat), 4), codes={str(k): v for k, v in codes.items()})
    print(json.dumps(res), flush=True)
    open(OUT + '.partial', 'a').write(json.dumps(res) + chr(10)); return res

def login_req(email, role):
    async def f(c, a):
        if a is None: return None
        return await c.post("/auth/login", json={"email": email, "password": email.split("@")[0][::-1], "role": role})
    return f

def get_req(path, email, role):
    async def f(c, a):
        if a is None: return {"Authorization": f"Bearer {await token(c, email, role)}"}
        return await c.get(path, headers=a[0])
    return f

def submit_cancel(email, role):
    async def f(c, a):
        if a is None: return {"Authorization": f"Bearer {await token(c, email, role)}"}
        h, i, n = a
        r = await c.post("/jobs", headers=h, json={"gpus": 1, "walltime": "00:30", "client_request_id": f"{LABEL}-{time.time_ns()}-{i}-{n}"})
        if r.status_code >= 400: return r
        return await c.post(f"/jobs/{r.json()['request_id']}/cancel", headers=h)
    return f

async def main():
    results = []
    async with httpx.AsyncClient(base_url=BASE) as c:
        h = {"Authorization": f"Bearer {await token(c, 'alice@iitk.ac.in', 'STUDENT')}"}
        one = (await c.get("/jobs", params={"limit": 1}, headers=h)).json()[0]["id"]
    plan = [
        ("GET /app (static shell)", get_req("/app/", "alice@iitk.ac.in", "STUDENT"), [1, 16, 64]),
        ("POST /auth/login", login_req("alice@iitk.ac.in", "STUDENT"), [1, 8, 32]),
        ("GET /jobs/{id}", get_req(f"/jobs/{one}", "alice@iitk.ac.in", "STUDENT"), [1, 16, 64]),
        ("GET /jobs?limit=100", get_req("/jobs?limit=100", "alice@iitk.ac.in", "STUDENT"), [1, 8, 32]),
        ("POST /jobs + cancel", submit_cancel("bob@iitk.ac.in", "RESEARCHER"), [1, 8]),
    ]
    only = sys.argv[4].split(",") if len(sys.argv) > 4 else None
    for name, f, concs in plan:
        if only and not any(o in name for o in only): continue
        for conc in concs:
            results.append(await scenario(name, conc, f))
    json.dump(dict(label=LABEL, base=BASE, duration_s=DUR, results=results), open(OUT, "w"), indent=1)

asyncio.run(main())
