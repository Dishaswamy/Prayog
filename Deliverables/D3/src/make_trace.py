"""Requirements -> Jira -> GitHub -> tests matrix, built from the backlog import, the Jira export,
the repository at C:/Users/Areen/Prayog-final and the junit results."""
import csv, re, subprocess, json, sys
from pathlib import Path
from collections import defaultdict
ROOT = Path(r"C:/Users/Areen/Prayog-final")
HERE = Path(__file__).resolve().parents[1]
JIRA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:/Users/Areen/Downloads/CS455 project D1/Jira.csv")
plan = list(csv.DictReader(open(HERE.parent / "PRAYOG_backlog_jira_import.csv", encoding="utf-8-sig")))
jira = {r["Issue key"]: r for r in csv.DictReader(open(JIRA, encoding="utf-8-sig", errors="replace"))}
RID = re.compile(r"\b(?:FR|NFR)-[A-Z]+-\d+\b")

def files(globs):
    out = []
    for g in globs:
        out += [p for p in ROOT.glob(g) if "__pycache__" not in p.as_posix()]
    return out
src = files(["backend/**/*.py", "steward/src/**/*.py", "cli/src/**/*.py", "backend/web/*.js"])
src = [p for p in src if "/tests" not in p.as_posix() and "tests_steward_check" not in p.as_posix()]
tests = files(["backend/tests/*.py", "steward/tests/**/*.py", "cli/tests/*.py"])
def index(paths):
    idx = defaultdict(set)
    for p in paths:
        for rid in set(RID.findall(p.read_text(encoding="utf-8", errors="ignore"))):
            idx[rid].add(p.relative_to(ROOT).as_posix())
    return idx
in_src, in_tests = index(src), index(tests)
log = subprocess.run(["git", "-C", str(ROOT), "log", "origin/main", "--format=%h%x09%an%x09%ad%x09%s", "--date=short"],
                     capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
# Until 15 Oct the ledger work was done in the working repository, whose commits cite *plan* keys
# (Jira key = plan key + 4); from 16 Oct every branch and commit uses Jira keys.
cites = defaultdict(list)
for line in log:
    h, author, day, subject = line.split("\t", 3)
    shift = 4 if author == "nideesh-iitk" and day <= "2026-10-15" else 0
    for n in set(re.findall(r"PRAYOG-(\d+)", subject)):
        cites[f"PRAYOG-{int(n) + shift}"].append(h)
def commits_for(key):
    return cites.get(key, [])

# junit pass/fail per test file
status = defaultdict(lambda: [0, 0])
for jx in sys.argv[2:]:
    import xml.etree.ElementTree as ET
    for tc in ET.parse(jx).iter("testcase"):
        parts = tc.get("classname", "").split(".")
        mod = next((x for x in parts if x.startswith("test_")), parts[0])
        if "cli" in Path(jx).stem: mod = "cli/" + mod
        if "steward_integration" in Path(jx).stem: mod = "tests_steward_check"
        if tc.find("skipped") is not None: continue
        bad = tc.find("failure") is not None or tc.find("error") is not None
        status[mod][1 if bad else 0] += 1

rows = []
for p in plan:
    if p["Issue Type"] == "Epic":
        continue
    n = int(p["Issue Key"].split("-")[1]) + 4
    key = f"PRAYOG-{n}"
    j = jira.get(key, {})
    rids = sorted(set(RID.findall(p["Description"])), key=lambda r: (r.split("-")[1], int(r.split("-")[2])))
    s = sorted(set().union(*[in_src.get(r, set()) for r in rids])) if rids else []
    t = sorted(set().union(*[in_tests.get(r, set()) for r in rids])) if rids else []
    res = [status.get(Path(x).stem) for x in t]
    passed = sum(r[0] for r in res if r); failed = sum(r[1] for r in res if r)
    rows.append(dict(key=key, summary=j.get("Summary") or p["Summary"], type=p["Issue Type"], epic=p["Component"],
                     reqs=rids, status=j.get("Status", "?"), assignee=j.get("Assignee", ""),
                     commits=commits_for(key), src=s, tests=t, passed=passed, failed=failed))
json.dump(rows, open(HERE / "evidence" / "traceability.json", "w"), indent=1)
have_req = sum(1 for r in rows if r["reqs"]); have_src = sum(1 for r in rows if r["src"]); have_t = sum(1 for r in rows if r["tests"])
print(len(rows), "items;", have_req, "with requirement IDs;", have_src, "traced to code;", have_t, "traced to tests")
print("requirement IDs cited in code:", len(in_src), "in tests:", len(in_tests))

# ── markdown matrix ──────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from trace_map import T
merges = subprocess.run(["git", "-C", str(ROOT), "log", "origin/main", "--merges", "--format=%s"], capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
prs = defaultdict(set)
for m in merges:
    g = re.match(r"Merge pull request #(\d+) from (\S+)", m)
    if not g:
        continue
    num, ref = int(g.group(1)), g.group(2)
    k = re.search(r"prayog-(\d+)", ref, re.I)
    if k:
        n = int(k.group(1)) + (4 if ref.startswith("baymaxisme/") else 0)   # the working repo used plan keys
        prs[n].add(f"{'wr' if ref.startswith('baymaxisme/') else ''}#{num}")
for n in list(range(93, 103)): prs[n].add("#11")
for n in (26, 31): prs[n].add("#19")
prs[53].add("#21"); prs[52].add("#22")
for n in (30, 44, 45): prs[n] |= {"#23", "#24"}
for n in (58, 71, 72): prs[n].add("#25")
for n in (54, 62, 80, 86, 88, 94, 95, 98): prs[n].add("#26")
for n in range(83, 93): prs[n].add("#12")

def res(mods):
    p = f = 0; missing = []
    for m in mods:
        r = status.get(m) or status.get(Path(m).stem)
        if r: p += r[0]; f += r[1]
        else: missing.append(m)
    return p, f, missing

out = ["| Jira | Story / task | Requirement(s) | Status | Code (files citing the requirement) | GitHub | Automated tests | Result 6 Nov | Other evidence |",
       "|---|---|---|---|---|---|---|---|---|"]
gaps = []
for r in rows:
    n = int(r["key"].split("-")[1])
    tests_, other = T.get(n, ([], ""))
    p, f, missing = res(tests_)
    result = (f"{p} pass" + (f", {f} fail" if f else "")) if tests_ else "—"
    gh = ", ".join(sorted(prs.get(n, set()), key=lambda s: int(s.strip("wr#")))) or (", ".join(r["commits"][:3]) if r["commits"] else "—")
    code = "<br>".join("`" + Path(s).name + "`" for s in r["src"][:3]) + (" …" if len(r["src"]) > 3 else "") if r["src"] else "—"
    if r["status"] == "Done" and not tests_:
        gaps.append(r["key"])
    out.append(f"| {r['key']} | {r['summary']} | {', '.join(r['reqs']) or '—'} | {r['status']} | {code} | {gh} | "
               f"{', '.join('`'+t+'`' for t in tests_) or '—'} | {result} | {other} |")
(HERE / "evidence" / "traceability_matrix.md").write_text("\n".join(out), encoding="utf-8")
print("Done without automated test:", gaps)
