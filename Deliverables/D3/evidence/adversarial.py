"""Black-box adversarial probes against a running Prayog API (test system, test accounts only).
Usage: python adversarial.py BASE_URL JWT_TEST_SECRET OUT.json
Each probe states the expected safe behaviour; PASS means the API behaved that way."""
import base64, json, sys, time
from datetime import datetime, timedelta, timezone
import httpx, jwt

BASE, SECRET, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
c = httpx.Client(base_url=BASE, timeout=30)
results = []


def pw(email): return email.split("@")[0][::-1]


def login(email, role):
    r = c.post("/auth/login", json={"email": email, "password": pw(email), "role": role}); r.raise_for_status()
    return r.json()["access_token"]


def H(tok): return {"Authorization": f"Bearer {tok}"}


def probe(pid, category, attack, expected, ok, observed):
    results.append(dict(id=pid, category=category, attack=attack, expected=expected, observed=observed, result="PASS" if ok else "FAIL"))
    print(f"{pid} {'PASS' if ok else 'FAIL'} {attack} -> {observed}")


alice, bob = login("alice@iitk.ac.in", "STUDENT"), login("bob@iitk.ac.in", "RESEARCHER")
me_alice = c.get("/auth/me", headers=H(alice)).json()
# alice's seeded queue is at the 50-job limit: free room so input probes test validation, not the limit
for j in [j for j in c.get("/jobs?limit=500", headers=H(alice)).json() if j["state"] == "QUEUED"][:20]:
    c.post(f"/jobs/{j['id']}/cancel", headers=H(alice))
job = c.post("/jobs", headers=H(alice), json={"gpus": 1, "walltime": "00:30", "client_request_id": f"adv-{time.time_ns()}"}).json()["request_id"]

# ── broken access control ────────────────────────────────────────────
r = c.get(f"/jobs/{job}", headers=H(bob))
probe("A01", "IDOR", "bob reads alice's job by ID", "404, same as a missing job", r.status_code == 404, r.status_code)
r2 = c.get("/jobs/99999999", headers=H(bob))
probe("A02", "IDOR", "existence oracle: foreign vs missing job", "identical status and body", (r.status_code, r.text) == (r2.status_code, r2.text), f"{r.status_code}/{r2.status_code}")
r = c.post(f"/jobs/{job}/cancel", headers=H(bob)); st = c.get(f"/jobs/{job}", headers=H(alice)).json()["state"]
probe("A03", "IDOR", "bob cancels alice's job", "refused; job unchanged", r.status_code in (403, 404) and st != "CANCELLED", f"{r.status_code}, job {st}")
r1 = c.post(f"/jobs/{job}/cancel", headers=H(bob)); r2 = c.post("/jobs/99999999/cancel", headers=H(bob))
probe("A03b", "IDOR", "existence oracle on cancel: foreign vs missing job", "identical status (404), as for reads", r1.status_code == r2.status_code, f"{r1.status_code}/{r2.status_code}")
for path in ("/admin/pool", "/admin/nodes", "/admin/alerts"):
    r = c.get(path, headers=H(alice))
    probe("A04", "privilege escalation", f"student GET {path}", "403", r.status_code == 403, r.status_code)
r = c.post("/admin/nodes", headers=H(alice), json={"name": "evil", "owner_lab_id": 1, "unit_count": 8, "donation_mode": "shared"})
probe("A05", "privilege escalation", "student registers a GPU node", "403/422, nothing created", r.status_code in (403, 422), r.status_code)
r = c.get("/agent/allocations", headers=H(alice))
probe("A06", "privilege escalation", "user token on node-agent API", "401/403", r.status_code in (401, 403), r.status_code)

# ── token forgery and session handling ───────────────────────────────
hdr = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
body = base64.urlsafe_b64encode(json.dumps({"sub": jwt.decode(alice, SECRET, algorithms=["HS256"])["sub"], "exp": 9999999999}).encode()).rstrip(b"=").decode()
r = c.get("/jobs", headers=H(f"{hdr}.{body}."))
probe("A07", "token forgery", "alg=none token", "401", r.status_code == 401, r.status_code)
r = c.get("/jobs", headers=H(jwt.encode({"sub": jwt.decode(alice, SECRET, algorithms=["HS256"])["sub"], "exp": 9999999999}, "guessed-secret", algorithm="HS256")))
probe("A08", "token forgery", "token signed with a guessed key", "401", r.status_code == 401, r.status_code)
p = alice.split("."); claims = json.loads(base64.urlsafe_b64decode(p[1] + "=="))
claims["sub"] = str(int(claims["sub"]) + 1)
tampered = p[0] + "." + base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + "." + p[2]
r = c.get("/jobs", headers=H(tampered))
probe("A09", "token forgery", "payload sub changed, signature kept", "401", r.status_code == 401, r.status_code)
good = jwt.decode(alice, SECRET, algorithms=["HS256"])
expired = jwt.encode({**good, "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}, SECRET, algorithm="HS256")
r = c.get("/jobs", headers=H(expired))
probe("A10", "session", "expired token (correctly signed)", "401", r.status_code == 401, r.status_code)
admin_claim = jwt.encode({**good, "role": "ADMIN"}, SECRET, algorithm="HS256")
r = c.get("/admin/pool", headers=H(admin_claim))
probe("A11", "privilege escalation", "validly signed token claiming role ADMIN for a student", "403 (role read from DB)", r.status_code == 403, r.status_code)
t = login("alice@iitk.ac.in", "STUDENT"); c.post("/auth/logout", headers=H(t)); r = c.get("/jobs", headers=H(t))
probe("A12", "session", "token reused after logout", "401", r.status_code == 401, r.status_code)
r = c.get("/jobs", headers=H(alice))
probe("A13", "session", "a second login survives the first one's logout", "200", r.status_code == 200, r.status_code)

# ── injection and input validation ───────────────────────────────────
for pid, email in (("A14", "' OR '1'='1' --"), ("A14", "alice@iitk.ac.in' --"), ("A14", "admin@iitk.ac.in\"; DROP TABLE users; --")):
    r = c.post("/auth/login", json={"email": email, "password": "x' OR '1'='1", "role": "ADMIN"})
    probe(pid, "SQL injection", f"login email {email!r}", "401/422, never 200 or 500", r.status_code in (401, 422), r.status_code)
r = c.post("/auth/login", json={"email": "alice@iitk.ac.in", "password": "A" * 100_000, "role": "STUDENT"})
probe("A15", "input validation", "100 kB password (hashing DoS)", "422 before hashing", r.status_code == 422, r.status_code)
for g, w in ((-1, "00:30"), (0, "00:30"), (10**9, "00:30"), (1, "999:99"), (1, "00:00"), ("1; DROP", "00:30"), (1, "1e9")):
    r = c.post("/jobs", headers=H(alice), json={"gpus": g, "walltime": w, "client_request_id": f"adv-bad-{time.time_ns()}"})
    probe("A16", "input validation", f"gpus={g!r} walltime={w!r}", "4xx with a reason", 400 <= r.status_code < 500, f"{r.status_code} {r.json().get('code', '')}")
r = c.post("/jobs", headers=H(alice), json={"gpus": 1, "walltime": "00:30", "client_request_id": f"adv-mass-{time.time_ns()}",
                                            "lab_id": 999, "account_type": "COURSE", "state": "RUNNING", "user_id": 1})
ok = r.status_code in (201, 422)
if r.status_code == 201:
    j = c.get(f"/jobs/{r.json()['request_id']}", headers=H(alice)).json(); ok = j["state"] != "RUNNING"
probe("A17", "mass assignment", "submit with lab_id/state/user_id fields", "extra fields ignored or refused", ok, r.status_code)

# ── path traversal and static files ──────────────────────────────────
for path in ("/app/../main.py", "/app/%2e%2e/main.py", "/app/..%2fmain.py", "/app/%2e%2e%2f%2e%2e%2f.env", "/app/web.css/../../database.py"):
    r = c.get(path)
    leaked = "SECRET" in r.text or "create_engine" in r.text or "import " in r.text[:200]
    probe("A18", "path traversal", f"GET {path}", "no source or secrets served", not leaked, r.status_code)

# ── brute force and CORS ─────────────────────────────────────────────
codes = [c.post("/auth/login", json={"email": "prof2@iitk.ac.in", "password": f"wrong-{i}", "role": "PROFESSOR"}).status_code for i in range(11)]
r = c.post("/auth/login", json={"email": "prof2@iitk.ac.in", "password": pw("prof2@iitk.ac.in"), "role": "PROFESSOR"})
probe("A19", "brute force", "11 wrong passwords then the right one", "right password refused during lockout, same 401", r.status_code == 401 and set(codes) == {401}, f"{set(codes)} then {r.status_code}")
r = c.options("/jobs", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
probe("A20", "CORS", "preflight from an unlisted origin", "no Access-Control-Allow-Origin", "access-control-allow-origin" not in r.headers, r.headers.get("access-control-allow-origin"))
r = c.get("/app/")
missing = [h for h in ("x-content-type-options", "x-frame-options", "content-security-policy", "referrer-policy") if h not in r.headers]
probe("A21", "hardening", "security headers on the web app", "nosniff, frame and CSP headers present", not missing, f"missing: {missing}" if missing else "all present")
r = c.get("/jobs/abc", headers=H(alice))
probe("A22", "error handling", "malformed path parameter", "422 without a stack trace", r.status_code == 422 and "Traceback" not in r.text, r.status_code)

json.dump(results, open(OUT, "w"), indent=1)
print(f"{sum(r['result'] == 'PASS' for r in results)}/{len(results)} PASS")
