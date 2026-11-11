r"""
Live demo: unauthorised access is refused, against the real server. Read-only: it changes nothing.

    python demo/security_demo.py

Uses the student login saved by `prayog login`. Each probe is something the caller must NOT be able
to do; the server's answer is printed next to what it should be.
"""

import json
import os
import sys
from pathlib import Path

import httpx

DENIED = {401, 403, 404}


def main() -> None:
    creds = json.loads((Path(os.environ.get("PRAYOG_CONFIG_DIR", Path.home() / ".prayog")) / "credentials.json")
                       .read_text())
    http = httpx.Client(base_url=creds["api_url"], timeout=90)
    student = {"Authorization": f"Bearer {creds['token']}"}
    if http.get("/auth/me", headers=student).status_code != 200:
        sys.exit("The saved student login has expired. Run `prayog login` first.")
    forged = creds["token"][:-6] + ("AAAAAA" if not creds["token"].endswith("AAAAAA") else "BBBBBB")

    probes = [
        ("No login: list jobs", "GET", "/jobs", {}, None),
        ("Forged token (signature altered)", "GET", "/auth/me", {"Authorization": f"Bearer {forged}"}, None),
        ("Student reads the admin pool", "GET", "/admin/pool", student, None),
        ("Student registers a GPU node", "POST", "/admin/nodes", student,
         {"name": "evil", "owner_lab_id": 1, "donation_mode": "shared", "unit_count": 8}),
        ("Student cancels any job as admin", "POST", "/admin/jobs/1/cancel", student, {"reason": "x"}),
        ("Student approves an access request", "POST", "/access-requests/1/decision", student, {"decision": "APPROVED"}),
        ("Student lists everyone's access requests", "GET", "/access-requests", student, None),
        ("Student opens the professor view", "GET", "/mlflow/professor/projects", student, None),
        ("Student reads a job that is not theirs", "GET", "/jobs/999999", student, None),
        ("Node agent with a fake credential", "POST", "/agent/heartbeat", {"X-Node-Credential": "not-a-real-credential"},
         {"unit_count": 8, "running_allocation_ids": []}),
        ("Fake node asks for someone's code", "GET", "/agent/allocations/1/workload",
         {"X-Node-Credential": "not-a-real-credential"}, None),
    ]
    print(f"Server: {creds['api_url']}   caller: {creds['email']} ({creds['role']})\n")
    print(f"{'PROBE':44s} {'ANSWER':7s} RESULT")
    failures = 0
    for name, method, path, headers, body in probes:
        r = http.request(method, path, headers=headers, json=body)
        ok = r.status_code in DENIED
        failures += not ok
        detail = ""
        try:
            detail = str(r.json().get("detail", ""))[:60]
        except ValueError:
            pass
        print(f"{name:44s} {r.status_code:<7d} {'REFUSED' if ok else 'ALLOWED (!)'}  {detail}")
    print(f"\n{len(probes) - failures}/{len(probes)} unauthorised actions refused.")


if __name__ == "__main__":
    main()
